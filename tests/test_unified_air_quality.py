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
    assert band_for_score(100) == (5, "Good")
    assert band_for_score(80) == (5, "Good")
    assert band_for_score(79.9) == (4, "Fair")
    assert band_for_score(60) == (4, "Fair")
    assert band_for_score(40) == (3, "Moderate")
    assert band_for_score(20) == (2, "Poor")
    assert band_for_score(0) == (1, "Very Poor")


def test_sgp30_is_absolute_and_uba_banded():
    good = sgp30_air_quality(30)        # < 65 ppb → UBA level 1
    assert good["air_quality_basis"] == "absolute"
    assert good["air_quality_band_label"] == "Good"
    assert good["uba_level"] == 1
    # band boundaries fall on the UBA thresholds
    assert sgp30_air_quality(64)["air_quality_band_label"] == "Good"
    assert sgp30_air_quality(200)["air_quality_band_label"] == "Fair"       # 65..220
    assert sgp30_air_quality(400)["air_quality_band_label"] == "Moderate"   # 220..660
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
    assert r["air_quality_band_label"] == "Good"
    assert sgp40_air_quality(100)["air_quality_band_label"] == "Good"       # at baseline
    assert sgp40_air_quality(150)["air_quality_band_label"] == "Fair"       # 100..200
    assert sgp40_air_quality(250)["air_quality_band_label"] == "Moderate"   # 200..300
    assert sgp40_air_quality(450)["air_quality_band_label"] == "Very Poor"  # > 400
    assert "not comparable to other rooms" in sgp40_air_quality(150)["explanation"]
    assert sgp40_air_quality(0)["air_quality_conf"] == "warmup"


def test_bme680_is_relative_declamped_ratio():
    base = 100_000
    clean = bme680_air_quality(gas_ohm=98_000, ambient_rh_pct=40.0, gas_baseline_ohm=base)
    assert clean["air_quality_basis"] == "relative"
    assert clean["air_quality_band_label"] == "Good"          # r ~0.98 → Good
    dirty = bme680_air_quality(gas_ohm=40_000, ambient_rh_pct=40.0, gas_baseline_ohm=base)
    assert dirty["air_quality"] < clean["air_quality"]        # resistance dropped → worse
    assert dirty["air_quality_band_label"] in ("Moderate", "Poor")
    # gates
    assert bme680_air_quality(50_000, 40.0, None)["air_quality_conf"] == "burn_in"
    assert bme680_air_quality(50_000, 40.0, base, gas_valid=0)["air_quality_conf"] == "stale"


def test_dispatch_by_device_type():
    assert air_quality_for("sgp30_gas", {"tvoc": 30})["family"] == "SGP30"
    assert air_quality_for("sgp40_gas", {"voc_index": 120})["family"] == "SGP40"
    assert air_quality_for("bme680_gas", {"gas_ohm": 98_000},
                           ambient_rh_pct=40.0, gas_baseline_ohm=100_000)["family"] == "BME680"
    assert air_quality_for("meter_pro", {"temperature_c": 21}) is None    # not a gas node
