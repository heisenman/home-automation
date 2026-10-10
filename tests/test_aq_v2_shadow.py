"""BME680 v2 shadow air quality (ADR-0035 amendment 2026-10-10) — the 48 h time-window baseline and the
absolute-humidity correction from the node's OWN die T/RH (no borrowed reference meter)."""
import sqlite3
import tempfile
import time
from pathlib import Path

from server.gas_compensation import (BME_V2_WINDOW_S, absolute_humidity_gm3, bme680_air_quality_v2,
                                     bme680_corrected_log_r, bme680_v2_baseline)
from server.maintenance import gas_quality_persist as P
from server.storage.rollup import RUNG_DDL

FULL = BME_V2_WINDOW_S // 60


def test_absolute_humidity_is_invariant_to_self_heating():
    # 22 °C / 45 %RH ambient, seen by a die ~8 °C warmer: RH drops, water vapour does not
    amb = absolute_humidity_gm3(22.0, 45.0)
    assert 8.5 < amb < 9.0
    die_rh = 45.0 * absolute_humidity_gm3(22.0, 100.0) / absolute_humidity_gm3(30.0, 100.0)
    assert abs(absolute_humidity_gm3(30.0, die_rh) - amb) < 0.15


def test_wetter_air_reads_as_higher_corrected_resistance():
    # MOX R falls as water vapour rises; the correction must undo that, not add to it
    dry = bme680_corrected_log_r(100_000, 30.0, 25.0)
    wet = bme680_corrected_log_r(100_000, 30.0, 45.0)
    assert wet > dry


def test_baseline_needs_coverage():
    samples = [(100_000, 30.0, 30.0)] * 100
    base, cov = bme680_v2_baseline(samples, FULL)
    assert cov < 0.5
    r = bme680_air_quality_v2(100_000, 30.0, 30.0, base, cov)
    assert r["air_quality"] is None and r["air_quality_conf"] == "burn_in"


def test_slow_drop_is_not_absorbed_like_v1():
    """The v1 defect: a 2 h baseline swallowed any slow decline. With 48 h of clean air at 200 kΩ, a reading
    at 120 kΩ must band Fair, not Excellent."""
    samples = [(200_000, 30.0, 30.0)] * FULL
    base, cov = bme680_v2_baseline(samples, FULL)
    clean = bme680_air_quality_v2(200_000, 30.0, 30.0, base, cov)
    dirty = bme680_air_quality_v2(120_000, 30.0, 30.0, base, cov)
    assert clean["air_quality_band_label"] == "Excellent"
    assert dirty["ratio"] == 0.6 and dirty["air_quality_band_label"] == "Fair"   # r 0.5–0.7
    assert dirty["air_quality_basis"] == "relative"


def test_invalid_gas_is_stale():
    assert bme680_air_quality_v2(100_000, 30.0, 30.0, 11.5, 1.0, gas_valid=0)["air_quality"] is None


def test_sampler_v2_reads_the_rung_window():
    now = time.time()
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    hot = sqlite3.connect(":memory:")
    hot.execute("CREATE TABLE readings (ts TEXT, device_id TEXT, metric TEXT, value REAL, authoritative INT)")
    for m, v in (("gas_ohm", 120_000), ("temperature_c", 30.0), ("humidity_pct", 30.0), ("gas_valid", 1)):
        hot.execute("INSERT INTO readings VALUES (?,?,?,?,1)", (iso, "gas_x", m, v))
    with tempfile.TemporaryDirectory() as td:
        rp = Path(td) / "rungs.db"
        rc = sqlite3.connect(rp)
        rc.executescript(RUNG_DDL)
        start = int(now) - BME_V2_WINDOW_S + 120
        rows = [("1min", "gas_x", m, b, None, None, v, 1, v)
                for b in range(start, int(now), 60)
                for m, v in (("gas_ohm", 200_000), ("temperature_c", 30.0), ("humidity_pct", 30.0))]
        rc.executemany("INSERT INTO rung VALUES (?,?,?,?,?,?,?,?,?)", rows)
        rc.commit()
        rc.close()
        assert P._bme680_v2(hot, "gas_x", now, rungs_path=rp) == 50.0       # r = 0.6 → score 50
        assert P._bme680_v2(hot, "gas_x", now, rungs_path=Path(td) / "missing.db") is None


def test_pm25_bands_on_epa_2024_breakpoints():
    from server.gas_compensation import pm25_air_quality
    assert pm25_air_quality(1.0)["air_quality_band_label"] == "Excellent"     # ≤ 9.0 Good (EPA)
    assert pm25_air_quality(9.0)["air_quality"] == 80.0
    assert pm25_air_quality(20.0)["air_quality_band_label"] == "Good"         # Moderate (EPA)
    assert pm25_air_quality(40.0)["air_quality_band_label"] == "Fair"         # USG
    assert pm25_air_quality(100.0)["air_quality_band_label"] == "Poor"        # Unhealthy
    assert pm25_air_quality(300.0)["air_quality"] == 0.0                      # beyond Very Unhealthy
    assert pm25_air_quality(5.0)["air_quality_basis"] == "absolute"
    assert pm25_air_quality(5.0, n_buckets=2)["air_quality"] is None          # too sparse an hour


def test_sampler_pm25_is_count_weighted_hour_mean():
    now = time.time()
    with tempfile.TemporaryDirectory() as td:
        rp = Path(td) / "rungs.db"
        rc = sqlite3.connect(rp)
        rc.executescript(RUNG_DDL)
        b0 = int(now) - 1800
        rc.executemany("INSERT INTO rung VALUES ('1min','p','pm25_ugm3',?,NULL,NULL,?,?,NULL)",
                       [(b0, 9.0, 3), (b0 + 60, 9.0, 3), (b0 + 120, 35.4, 0),          # vcount 0 → weight 1
                        (int(now) - 7200, 500.0, 5)])                                # outside the hour
        rc.commit()
        rc.close()
        # (9*3 + 9*3 + 35.4*1) / 7 = 12.77 µg/m³ → Good, just under 80
        assert 75.0 < P._pm25_score("p", now, rungs_path=rp) < 80.0
