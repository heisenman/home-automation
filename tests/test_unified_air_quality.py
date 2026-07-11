"""Unified air-quality index (ADR-0035) — pins the per-family transfer functions, the banded score, and the
absolute/relative basis flag across SGP30 / SGP40 / BME680."""
from server.gas_compensation import (
    air_quality_for,
    band_for_score,
    bme680_air_quality,
    sgp30_air_quality,
    sgp40_air_quality,
)


def test_band_boundaries():
    assert band_for_score(100) == (5, "Excellent")
    assert band_for_score(80) == (5, "Excellent")
    assert band_for_score(79.9) == (4, "Good")
    assert band_for_score(60) == (4, "Good")
    assert band_for_score(40) == (3, "Fair")
    assert band_for_score(20) == (2, "Poor")
    assert band_for_score(0) == (1, "Very Poor")


def test_sgp30_is_absolute_and_uba_banded():
    good = sgp30_air_quality(30)        # < 65 ppb → UBA level 1
    assert good["air_quality_basis"] == "absolute"
    assert good["air_quality_band_label"] == "Excellent"
    assert good["uba_level"] == 1
    # band boundaries fall on the UBA thresholds
    assert sgp30_air_quality(64)["air_quality_band_label"] == "Excellent"
    assert sgp30_air_quality(200)["air_quality_band_label"] == "Good"       # 65..220
    assert sgp30_air_quality(400)["air_quality_band_label"] == "Fair"       # 220..660
    assert sgp30_air_quality(1500)["air_quality_band_label"] == "Poor"      # 660..2200
    assert sgp30_air_quality(3000)["air_quality_band_label"] == "Very Poor"
    # monotonic: more TVOC → lower (or equal) cleanliness score
    assert sgp30_air_quality(30)["air_quality"] > sgp30_air_quality(400)["air_quality"]


def test_sgp30_warmup_gate():
    r = sgp30_air_quality(0, eco2_ppm=400)     # both-at-floor → warming up
    assert r["air_quality_conf"] == "warmup"
    assert r["air_quality"] is None


def test_sgp40_is_relative_native_index():
    r = sgp40_air_quality(50)       # below baseline → clean
    assert r["air_quality_basis"] == "relative"
    assert r["air_quality_band_label"] == "Excellent"
    assert sgp40_air_quality(100)["air_quality_band_label"] == "Excellent"  # at baseline
    assert sgp40_air_quality(150)["air_quality_band_label"] == "Good"       # 100..200
    assert sgp40_air_quality(250)["air_quality_band_label"] == "Fair"       # 200..300
    assert sgp40_air_quality(450)["air_quality_band_label"] == "Very Poor"  # > 400
    assert "not comparable to other rooms" in sgp40_air_quality(150)["explanation"]
    assert sgp40_air_quality(0)["air_quality_conf"] == "warmup"


def test_bme680_is_relative_declamped_ratio():
    base = 100_000
    clean = bme680_air_quality(gas_ohm=98_000, ambient_rh_pct=40.0, gas_baseline_ohm=base)
    assert clean["air_quality_basis"] == "relative"
    assert clean["air_quality_band_label"] == "Excellent"     # r ~0.98 → Excellent
    dirty = bme680_air_quality(gas_ohm=40_000, ambient_rh_pct=40.0, gas_baseline_ohm=base)
    assert dirty["air_quality"] < clean["air_quality"]        # resistance dropped → worse
    assert dirty["air_quality_band_label"] in ("Fair", "Poor")
    # gates
    assert bme680_air_quality(50_000, 40.0, None)["air_quality_conf"] == "burn_in"
    assert bme680_air_quality(50_000, 40.0, base, gas_valid=0)["air_quality_conf"] == "stale"


def test_resolve_room_air_quality_worst_band_wins():
    from server.api.viewmodel import resolve_room_air_quality
    sensors = [
        {"device_id": "gas_a", "air_quality_report": {"air_quality_band": 5, "air_quality_band_label": "Excellent",
                                                       "air_quality_basis": "relative", "air_quality": 90}},
        {"device_id": "gas_b", "air_quality_report": {"air_quality_band": 3, "air_quality_band_label": "Fair",
                                                       "air_quality_basis": "relative", "air_quality": 50}},
        {"device_id": "meter", "air_quality_report": None},          # non-gas → ignored
    ]
    r = resolve_room_air_quality(sensors)
    assert r["air_quality_band_label"] == "Fair" and r["source_device_id"] == "gas_b"   # worst wins
    assert r["multi"] is True
    # tie on band → absolute basis wins
    tie = resolve_room_air_quality([
        {"device_id": "rel", "air_quality_report": {"air_quality_band": 4, "air_quality_band_label": "Good",
                                                    "air_quality_basis": "relative", "air_quality": 70}},
        {"device_id": "abs", "air_quality_report": {"air_quality_band": 4, "air_quality_band_label": "Good",
                                                    "air_quality_basis": "absolute", "air_quality": 70}},
    ])
    assert tie["source_device_id"] == "abs"
    # no usable band (warmup/stale) → None, map omits the badge
    assert resolve_room_air_quality([{"device_id": "g", "air_quality_report":
                                      {"air_quality_band": None, "air_quality_conf": "warmup"}}]) is None
    assert resolve_room_air_quality([]) is None


def test_band_legend_is_the_lookup_table():
    from server.gas_compensation import band_legend
    leg = band_legend()
    assert [e["label"] for e in leg] == ["Excellent", "Good", "Fair", "Poor", "Very Poor"]   # cleanest→worst
    assert [e["band"] for e in leg] == [5, 4, 3, 2, 1]
    assert leg[0]["score_min"] == 80 and leg[0]["score_max"] == 100
    assert all("meaning" in e for e in leg)


def test_dispatch_by_device_type():
    assert air_quality_for("sgp30_gas", {"tvoc": 30})["family"] == "SGP30"
    assert air_quality_for("sgp40_gas", {"voc_index": 120})["family"] == "SGP40"
    assert air_quality_for("bme680_gas", {"gas_ohm": 98_000},
                           ambient_rh_pct=40.0, gas_baseline_ohm=100_000)["family"] == "BME680"
    assert air_quality_for("meter_pro", {"temperature_c": 21}) is None    # not a gas node
