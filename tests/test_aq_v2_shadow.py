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


def test_house_median_and_area_worst():
    from server.gas_compensation import area_worst, house_median
    a = area_worst({"k": 50, "m1": 90, "m2": 60, "b": 80}, {"k": "kitchen", "m1": "mech", "m2": "mech", "b": "bed"})
    assert a == {"kitchen": 50, "mech": 60, "bed": 80}
    assert house_median(a) == 60
    assert house_median({"a": 1, "b": 2}) is None                       # < 3 areas → no house


def test_aq_context_verdicts():
    from server.gas_compensation import aq_context
    room = aq_context(40, house=85, offset=0, typical=80)
    assert room["locality"] == "room" and room["timing"] == "unusual" and room["local_excess"] == -45
    assert "something local" in room["note"]
    # the bedroom-at-night pattern: local but recurring
    assert "recurring" in aq_context(55, house=85, offset=0, typical=58)["note"]
    house = aq_context(55, house=55, offset=0, typical=60)
    assert house["locality"] == "house" and house["timing"] == "typical"
    # a constant sensor-family offset is NOT "this room only"
    assert aq_context(77, house=85, offset=-8, typical=78)["locality"] == "none"
    assert aq_context(80, house=None, offset=None, typical=None)["timing"] == "unknown"


def test_sampler_contexts_from_hourly_rung():
    """10 days of hourly history: kitchen sits 10 below the house every hour (its offset), so a reading 10
    below is NOT local; a reading 40 below is."""
    now = time.time()
    areas = {"g_k": "kitchen", "g_o": "office", "g_b": "bed"}
    fams = {"g_k": "sgp40", "g_o": "sgp40", "g_b": "sgp30"}
    with tempfile.TemporaryDirectory() as td:
        rp = Path(td) / "rungs.db"
        rc = sqlite3.connect(rp)
        rc.executescript(RUNG_DDL)
        h0 = int(now) // 3600 * 3600
        rows = []
        for i in range(1, 240):
            b = h0 - i * 3600
            for d, v in (("g_k", 70.0), ("g_o", 80.0), ("g_b", 85.0)):
                rows.append(("1hour", d, "air_quality", b, None, None, v, 60, v))
        rc.executemany("INSERT INTO rung VALUES (?,?,?,?,?,?,?,?,?)", rows)
        rc.commit()
        rc.close()
        ctx = P._aq_contexts({"g_k": 70.0, "g_o": 80.0, "g_b": 85.0}, areas, fams, now, rungs_path=rp, tz="UTC")
        assert ctx["g_k"]["local_excess"] == 0.0 and ctx["g_k"]["locality"] == "none"
        assert ctx["g_k"]["timing"] == "typical"
        ctx = P._aq_contexts({"g_k": 40.0, "g_o": 80.0, "g_b": 85.0}, areas, fams, now, rungs_path=rp, tz="UTC")
        assert ctx["g_k"]["locality"] == "room" and ctx["g_k"]["timing"] == "unusual"


def test_ventilation_coupling_and_state():
    from server.room_ventilation import coupling, house_series, ventilation_state
    base = 1_000_000 // 900 * 900
    house_moves = {base + i * 900: 8.0 + (0.3 if i % 2 else 0.0) for i in range(9)}
    assert coupling({b: v + 1.0 for b, v in house_moves.items()}, house_moves) > 0.99   # moves with the house
    drifting = {base + i * 900: 8.0 + 0.05 * i for i in range(9)}
    assert abs(coupling(drifting, house_moves)) < 0.3                                    # drifts on its own
    assert coupling({base: 1.0}, {base: 1.0}) is None
    hs = house_series({"a": {1: 1.0}, "b": {1: 3.0}, "c": {1: 2.0}, "room": {1: 9.0}}, "room")
    assert hs == {1: 2.0}
    assert house_series({"a": {1: 1.0}, "room": {1: 9.0}}, "room") == {}                # < 3 others
    assert ventilation_state(0.05, 0.3)["state"] == "accumulating"
    assert ventilation_state(0.6, 0.3)["state"] == "mixed"
    assert ventilation_state(None, 0.3)["state"] == "unknown"


def test_room_meters_prefers_pro_and_skips_wet_rooms_and_gas():
    meta = {"bed": {"level": "main_floor", "type": "bedroom"}, "bath": {"level": "main_floor", "type": "bathroom"},
            "attic": {"level": "attic", "type": "monolithic"}}
    th = {"temperature_c": 21.0, "humidity_pct": 40.0}
    devs = [{"device_id": "meter_bed", "device_type": "switchbot_meter", "area": "bed", "ts": "2026-10-10T10:00:00Z", "metrics": th},
            {"device_id": "meter_pro_bed", "device_type": "switchbot_meter", "area": "bed", "ts": "2026-10-10T09:00:00Z", "metrics": th},
            {"device_id": "gas_bed", "device_type": "bme680_gas", "area": "bed", "ts": "2026-10-10T11:00:00Z", "metrics": th},
            {"device_id": "meter_bath", "device_type": "switchbot_meter", "area": "bath", "metrics": th},
            {"device_id": "meter_attic", "device_type": "switchbot_meter", "area": "attic", "metrics": th}]
    m = P._room_meters(devs, meta)
    assert list(m) == ["bed"] and m["bed"]["device_id"] == "meter_pro_bed"


def test_voc_index_port_matches_sensirion_c():
    """server/gas_index_algorithm.py vs the vendored C (firmware/components/sensirion_gas_index), compiled and
    run on the same trace at a 60 s interval on 2026-10-12: all 2880 outputs identical; these are checkpoints."""
    import math
    from server.gas_index_algorithm import VocIndex
    trace = [int(30000 + 60 * math.sin(i / 229.0) - (900 if 2400 <= i < 2440 else 0)) for i in range(2880)]
    alg = VocIndex(60.0)
    out = [alg.process(v) for v in trace]
    expect = {10: 100, 100: 95, 1000: 134, 2399: 129, 2410: 492, 2420: 492, 2439: 492, 2500: 130, 2879: 94}
    assert {i: out[i] for i in expect} == expect
    assert VocIndex(60.0).process(30000) == 0                         # blackout


def test_v3_bme680_on_the_voc_index_scale():
    from server.gas_compensation import bme680_air_quality_v3, bme680_voc_index
    steady = [(200_000, 30.0, 30.0)] * 2400
    idx, cov = bme680_voc_index(steady, 4320)
    assert 95 <= idx <= 105 and abs(cov - 2400 / 4320) < 1e-9
    rep = bme680_air_quality_v3(idx, cov)
    assert rep["air_quality_band_label"] in ("Excellent", "Good") and rep["air_quality_basis"] == "relative"
    # a 30-min resistance drop (VOC event) after the baseline has learned → well above 100 → Fair or worse
    idx, cov = bme680_voc_index(steady + [(100_000, 30.0, 30.0)] * 30, 2430)
    assert idx > 250 and bme680_air_quality_v3(idx, cov)["air_quality"] < 60
    # wetter air alone (same R corrected up) must NOT read as a VOC event
    idx_wet, _ = bme680_voc_index(steady + [(200_000, 30.0, 45.0)] * 30, 2430)
    assert idx_wet < 100
    assert bme680_air_quality_v3(idx, 0.2)["air_quality_conf"] == "burn_in"
    assert bme680_air_quality_v3(idx, 1.0, gas_valid=0)["air_quality"] is None


def test_sampler_v3_replays_the_rung():
    from server.gas_compensation import BME_V3_WINDOW_S
    now = time.time()
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

    def hot_with(gas_ohm):
        h = sqlite3.connect(":memory:")
        h.execute("CREATE TABLE readings (ts TEXT, device_id TEXT, metric TEXT, value REAL, authoritative INT)")
        for m, v in (("gas_ohm", gas_ohm), ("temperature_c", 30.0), ("humidity_pct", 30.0), ("gas_valid", 1)):
            h.execute("INSERT INTO readings VALUES (?,?,?,?,1)", (iso, "gas_x", m, v))
        return h
    with tempfile.TemporaryDirectory() as td:
        rp = Path(td) / "rungs.db"
        rc = sqlite3.connect(rp)
        rc.executescript(RUNG_DDL)
        start = int(now) - BME_V3_WINDOW_S + 120
        rc.executemany("INSERT INTO rung VALUES (?,?,?,?,?,?,?,?,?)",
                       [("1min", "gas_x", m, b, None, None, v, 1, v) for b in range(start, int(now) - 120, 60)
                        for m, v in (("gas_ohm", 200_000), ("temperature_c", 30.0), ("humidity_pct", 30.0))])
        rc.commit()
        rc.close()
        clean = P._bme680_v3(hot_with(200_000), "gas_x", now, rungs_path=rp)
        assert clean is not None and clean >= 75
        # the raw payload is newer than the last rung bucket → appended: a halved R moves the score at once
        hot = hot_with(100_000)
        assert P._bme680_v3(hot, "gas_x", now, rungs_path=rp) < clean
        assert P._bme680_v3(hot, "gas_x", now, rungs_path=Path(td) / "missing.db") is None


def test_tc_removes_a_pure_temperature_swing():
    """voc_raw that moves ONLY with room temperature (at the shadow coefficient) must index flat once corrected;
    uncorrected, the warm afternoon reads as a VOC event."""
    import math
    from server.gas_compensation import SGP_TC_K_T, sgp_air_quality_tc, sgp_tc_voc_index
    temps = [22.0 + 3.0 * max(0.0, math.sin((i % 1440) / 1440 * 2 * math.pi)) for i in range(4320)]
    raw = [30000 + SGP_TC_K_T * (t - 22.0) for t in temps]
    warm = max(range(2880, 4320), key=lambda i: temps[i])
    corrected, cov = sgp_tc_voc_index(list(zip(raw[:warm + 1], temps[:warm + 1])), 4320)
    uncorrected, _ = sgp_tc_voc_index([(r, 25.0) for r in raw[:warm + 1]], 4320)
    assert 90 <= corrected <= 110 and uncorrected > 130
    assert sgp_air_quality_tc(corrected, cov)["air_quality_band_label"] in ("Excellent", "Good")
    assert sgp_air_quality_tc(corrected, 0.1)["air_quality"] is None


def test_temp_source_is_same_room_only():
    th = {"temperature_c": 21.0, "humidity_pct": 40.0}
    devs = [{"device_id": "gas_k", "device_type": "sgp40_gas", "area": "kitchen", "metrics": {"voc_index": 100}},
            {"device_id": "meter_kitchen", "device_type": "switchbot_meter", "area": "kitchen", "ts": "2026-10-12T00:00:00Z", "metrics": th},
            {"device_id": "meter_pro_living", "device_type": "switchbot_meter", "area": "living", "ts": "2026-10-12T00:00:00Z", "metrics": th},
            {"device_id": "gas_mech", "device_type": "bme680_gas", "area": "mech", "ts": "2026-10-12T00:00:00Z",
             "metrics": {"gas_ohm": 300_000}},                  # as the sensor list shows it: no die T
            {"device_id": "gas_mech_sgp41", "device_type": "sgp41_gas", "area": "mech", "metrics": {"voc_index": 100}}]
    assert P._temp_source(devs, "kitchen", "gas_k")["device_id"] == "meter_kitchen"
    assert P._temp_source(devs, "mech", "gas_mech_sgp41")["device_id"] == "gas_mech"      # BME die as fallback
    assert P._temp_source(devs, "office", "gas_o") is None                                 # never another room


def test_sampler_tc_carries_temperature_forward():
    from server.gas_compensation import SGP_TC_WINDOW_S
    now = time.time()
    with tempfile.TemporaryDirectory() as td:
        rp = Path(td) / "rungs.db"
        rc = sqlite3.connect(rp)
        rc.executescript(RUNG_DDL)
        start = int(now) - SGP_TC_WINDOW_S + 60
        rc.executemany("INSERT INTO rung VALUES ('1min','g','voc_raw',?,NULL,NULL,30000,1,30000)",
                       [(b,) for b in range(start, int(now), 60)])
        rc.executemany("INSERT INTO rung VALUES ('1min','m','temperature_c',?,NULL,NULL,21.0,1,21.0)",
                       [(b,) for b in range(start, int(now), 300)])                    # a meter every 5 min
        rc.commit()
        rc.close()
        assert P._sgp_tc("g", "m", now, rungs_path=rp) == 80.0                           # flat → index 100 → 80
        assert P._sgp_tc("g", "nobody", now, rungs_path=rp) is None                      # no temperature → nothing


def test_ambient_targets_same_room_meter_only():
    from server.maintenance.sgp_ambient import node_for_device, push, targets
    reg = {"COFFICE_C6-GAS": {"device_id": "gas_c_office", "node_id": "coffice_c6"},
           "C6-BENCH-GAS": {"device_id": "gas_c_office"},                      # stale twin: node_id entry wins
           "S3-CRAWLSPACE-GAS": {"device_id": "gas_kitchen"},
           "SGP41_MECH-GAS": {"device_id": "gas_mech_sgp41", "node_id": "sgp41_mech"}}
    node_of = node_for_device(reg)
    assert node_of == {"gas_c_office": "coffice_c6", "gas_kitchen": "s3-crawlspace", "gas_mech_sgp41": "sgp41_mech"}
    th = {"temperature_c": 21.5, "humidity_pct": 44.0}
    devs = [{"device_id": "gas_c_office", "device_type": "sgp40_gas", "area": "c_office", "metrics": {}},
            {"device_id": "meter_pro_c_office", "device_type": "switchbot_meter_pro", "area": "c_office", "age_s": 30, "metrics": th},
            {"device_id": "gas_kitchen", "device_type": "sgp40_gas", "area": "kitchen", "metrics": {}},
            {"device_id": "meter_kitchen", "device_type": "switchbot_meter", "area": "kitchen", "age_s": 5000, "metrics": th},
            {"device_id": "gas_mech_sgp41", "device_type": "sgp41_gas", "area": "mech", "metrics": {}},
            {"device_id": "gas_mech", "device_type": "bme680_gas", "area": "mech", "metrics": th}]
    # kitchen meter too old; mech has only a BME die → nothing for either
    assert targets(devs, node_of) == [("coffice_c6", 21.5, 44.0, "meter_pro_c_office")]
    out = []
    sent = push(lambda t, pl: out.append((t, pl)), {"coffice_c6": {"cmd_secret": "k"}},
                targets(devs, node_of), now=1_800_000_000)
    assert sent == ["coffice_c6"] and out[0][0] == "home/edge/coffice_c6/ambient"
    import hashlib, hmac, json
    env = json.loads(out[0][1])
    assert hmac.new(b"k", env["p"].encode(), hashlib.sha256).hexdigest() == env["s"]
    assert json.loads(env["p"]) == {"op": "ambient", "ts": 1_800_000_000, "seq": 0, "t_c": 21.5, "rh_pct": 44.0,
                                    "src": "meter_pro_c_office"}
    assert push(lambda t, pl: out.append(t), {}, targets(devs, node_of)) == []        # no secret → nothing


def test_night_bases_and_shift():
    """01–05 h local buckets → one median a night (≥ 3 hours); shift vs the previous nights, + = more VOC."""
    from datetime import datetime, timezone
    from server.gas_compensation import night_bases, night_shift
    d0 = 1_790_000_000 // 86400 * 86400                          # a UTC midnight
    hourly = {}
    for k in range(10):
        for h in range(24):
            hourly[d0 + k * 86400 + h * 3600] = 33000.0 if h in (1, 2, 3, 4) else 30000.0   # daytime ignored
    hourly[d0 + 9 * 86400 + 2 * 3600] = 32400.0
    hourly[d0 + 9 * 86400 + 3 * 3600] = 32400.0
    hourly[d0 + 9 * 86400 + 4 * 3600] = 32400.0
    del hourly[d0 + 8 * 86400 + 1 * 3600], hourly[d0 + 8 * 86400 + 2 * 3600]   # night 8: only 2 hours → dropped
    b = night_bases(hourly, timezone.utc)
    last = datetime.fromtimestamp(d0 + 9 * 86400 + 7200, timezone.utc).date()
    assert len(b) == 9 and b[last] == 32400.0
    assert night_shift(b, last) == 600.0                          # voc_raw fell → more VOC → positive
    assert night_shift(b, last, cleaner_is_higher=False) == -600.0
    assert night_shift(b, min(b)) is None                         # no previous nights yet


def test_sampler_night_baseline_once_a_night():
    """voc_raw (SGP), BME680 gas_ohm and SGP30 tvoc each step toward more VOC on the last night → positive
    shifts, written once at 05:00 local; a second run the same day writes nothing."""
    from server.gas_compensation import BME_V3_TICKS_PER_LN
    import math
    d0 = 1_790_000_000 // 86400 * 86400
    now = d0 + 20 * 86400 + 6 * 3600                               # 06:00 UTC on day 20 → night 20 is complete
    with tempfile.TemporaryDirectory() as td:
        rp = Path(td) / "rungs.db"
        rc = sqlite3.connect(rp)
        rc.executescript(RUNG_DDL)
        rows = []
        for k in range(21):
            last = k == 20
            for h in (1, 2, 3, 4):
                b = d0 + k * 86400 + h * 3600
                for dev, m, v in (("g_sgp", "voc_raw", 32500.0 if last else 33000.0),
                                  ("g_bme", "gas_ohm", 150_000.0 if last else 300_000.0),
                                  ("g_bme", "temperature_c", 35.0), ("g_bme", "humidity_pct", 25.0),
                                  ("g_30", "tvoc", 600.0 if last else 150.0)):
                    rows.append(("1hour", dev, m, b, v, v, v, 60, v))
        rc.executemany("INSERT INTO rung VALUES (?,?,?,?,?,?,?,?,?)", rows)
        rc.commit()
        rc.close()
        hot = sqlite3.connect(":memory:")
        hot.execute("CREATE TABLE readings (ts TEXT, device_id TEXT, device_type TEXT, area TEXT, transport TEXT, "
                    "metric TEXT, value REAL, unit TEXT, schema_v INT, authoritative INT, PRIMARY KEY (device_id, ts, metric))")
        nodes = {"g_sgp": ("sgp41", "sgp41_gas", "a"), "g_bme": ("bme680", "bme680_gas", "b"),
                 "g_30": ("sgp30", "sgp30_gas", "c")}
        assert P._night_baseline(hot, nodes, now, rungs_path=rp, tz="UTC") == 3
        got = {(d, m): (ts, v) for ts, d, m, v in hot.execute("SELECT ts, device_id, metric, value FROM readings")}
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(d0 + 20 * 86400 + 5 * 3600))
        assert got[("g_sgp", "voc_night_base")] == (stamp, 32500.0)
        assert got[("g_sgp", "voc_night_shift")][1] == 500.0
        assert abs(got[("g_bme", "voc_night_shift")][1] - BME_V3_TICKS_PER_LN * math.log(2)) < 1.0
        assert got[("g_30", "voc_night_shift")][1] == 450.0                       # ppb, higher = more VOC
        assert P._night_baseline(hot, nodes, now + 600, rungs_path=rp, tz="UTC") == 0   # once a night
        # before 05:15 the latest complete night is the previous one — already in hot? no → it writes that one
        early = d0 + 20 * 86400 + 4 * 3600
        assert P._night_baseline(hot, nodes, early, rungs_path=rp, tz="UTC") == 3
        prev = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(d0 + 19 * 86400 + 5 * 3600))
        assert hot.execute("SELECT value FROM readings WHERE device_id='g_sgp' AND ts=? AND metric='voc_night_shift'",
                           (prev,)).fetchone()[0] == 0.0
