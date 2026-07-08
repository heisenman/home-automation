"""server/gas_compensation.py — humidity-compensated air-quality from BME680 gas + a reference sensor's
true ambient humidity. Pins the pure math (the viewmodel wiring is a thin shell over these)."""
from server.gas_compensation import (
    GAS_WEIGHT,
    HUM_WEIGHT,
    air_quality_index,
    clean_air_baseline,
    gas_score,
    humidity_score,
)


def test_humidity_score_peaks_at_40_and_zeros_at_extremes():
    assert humidity_score(40.0) == HUM_WEIGHT * 100          # ideal RH → full marks
    assert humidity_score(0.0) == 0.0
    assert humidity_score(100.0) == 0.0
    # symmetric-ish taper: 20% (half of 40) and 70% (halfway 40→100) both partial, below full
    assert 0 < humidity_score(20.0) < HUM_WEIGHT * 100
    assert 0 < humidity_score(70.0) < HUM_WEIGHT * 100


def test_gas_score_is_relative_to_baseline_and_capped():
    assert gas_score(50_000, 100_000) == GAS_WEIGHT * 100 * 0.5      # half the baseline
    assert gas_score(200_000, 100_000) == GAS_WEIGHT * 100           # above baseline → capped at full
    assert gas_score(50_000, 0) == 0.0                              # no baseline yet → 0
    assert gas_score(50_000, None) == 0.0


def test_air_quality_uses_reference_humidity_not_the_bme():
    # Clean air (gas at baseline) + ideal humidity → ~100.
    aq = air_quality_index(gas_ohm=100_000, ambient_rh_pct=40.0, gas_baseline_ohm=100_000)
    assert aq["air_quality"] == 100.0
    assert aq["gas_score"] == GAS_WEIGHT * 100 and aq["humidity_score"] == HUM_WEIGHT * 100
    # Same gas, but the TRUE ambient humidity is high (muggy) → lower score via the humidity term only.
    muggy = air_quality_index(gas_ohm=100_000, ambient_rh_pct=80.0, gas_baseline_ohm=100_000)
    assert muggy["gas_score"] == GAS_WEIGHT * 100                    # gas term unchanged
    assert muggy["air_quality"] < aq["air_quality"]                 # humidity dragged it down


def test_poor_air_low_gas_resistance_scores_low():
    aq = air_quality_index(gas_ohm=10_000, ambient_rh_pct=40.0, gas_baseline_ohm=100_000)
    assert aq["gas_score"] == GAS_WEIGHT * 100 * 0.1                # 10% of baseline
    assert aq["air_quality"] < 40


def test_clean_air_baseline_is_a_high_percentile():
    series = [10_000, 12_000, 50_000, 9_000, 100_000, 11_000, 10_500]   # 100k = a clean-air spike
    b = clean_air_baseline(series, percentile=0.95)
    assert b == 100_000                                            # tracks the cleanest recent air
    assert clean_air_baseline([]) is None
    assert clean_air_baseline([None, 0, -5]) is None               # nothing usable
