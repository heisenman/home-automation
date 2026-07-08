"""server/gas_compensation.py — humidity-compensated air-quality from a BME680 gas reading, using the TRUE
ambient humidity of a co-located reference sensor instead of the BME680's own self-heated value.

Why this exists
---------------
A BME680 on a tiny board reads its own die, which self-heats (~+10 °C from the gas heater + MCU proximity),
so its temperature/humidity are wrong for *ambient*. That matters because MOX gas resistance is strongly
humidity-dependent — the same VOC load reads a different resistance at 30 %RH vs 50 %RH — so turning gas_ohm
into an air-quality number needs the REAL ambient humidity. Bosch solves this inside BSEC (closed blob, no
clean RISC-V/offline path); this is the open equivalent, and it does one better: instead of the BME's own
bad humidity it uses the gas node's `ambient_ref` — the nearest reliable same-room T/H sensor (e.g.
gas_hbed → meter_pro_h_bed). Server-side sensor fusion, per Hugh's 2026-07-08 design.

Scope
-----
- GAS → a humidity-weighted score against a rolling clean-air baseline. This is what needs the reference.
- PRESSURE → NOT compensated here. The BME680's on-die barometer is correctly compensated at *die*
  temperature by the sensor's own polynomials (the pressure element sits on the same die, so die-temp IS the
  right temp for it). Re-compensating it with ambient temp would make it worse. Pressure passes through.

Pure functions; the caller (viewmodel) supplies gas_ohm + the ambient_ref's latest RH + a gas baseline it
computes from history. Constants are conventional (Bosch/community) and meant to be shadow-tuned on real data.
"""

HUM_REF_PCT = 40.0        # ideal indoor RH — the MOX baseline is defined around this
HUM_WEIGHT = 0.25         # humidity's share of the score (Bosch/community convention)
GAS_WEIGHT = 0.75         # gas resistance's share


def humidity_score(rh_pct: float) -> float:
    """0 .. HUM_WEIGHT*100. Full marks near 40 %RH, tapering linearly to 0 at 0 % and 100 %. Fed the TRUE
    ambient RH from the reference sensor — NOT the BME680's self-heated humidity."""
    rh = max(0.0, min(100.0, rh_pct))
    frac = rh / HUM_REF_PCT if rh < HUM_REF_PCT else (100.0 - rh) / (100.0 - HUM_REF_PCT)
    return frac * HUM_WEIGHT * 100.0


def gas_score(gas_ohm: float, gas_baseline_ohm: float) -> float:
    """0 .. GAS_WEIGHT*100. Gas resistance relative to the clean-air baseline (higher R = cleaner air),
    capped at the baseline. Returns 0 if there's no usable baseline yet."""
    if not gas_baseline_ohm or gas_baseline_ohm <= 0:
        return 0.0
    frac = min(max(gas_ohm, 0.0) / gas_baseline_ohm, 1.0)
    return frac * GAS_WEIGHT * 100.0


def air_quality_index(gas_ohm: float, ambient_rh_pct: float, gas_baseline_ohm: float) -> dict:
    """Humidity-compensated air-quality index, 0..100 (100 = cleanest). `ambient_rh_pct` MUST be the true
    ambient humidity from the reference sensor. Returns the index plus its components (for UI / tuning)."""
    h = humidity_score(ambient_rh_pct)
    g = gas_score(gas_ohm, gas_baseline_ohm)
    return {"air_quality": round(h + g, 1),
            "humidity_score": round(h, 1),
            "gas_score": round(g, 1),
            "gas_baseline_ohm": gas_baseline_ohm}


def clean_air_baseline(gas_ohm_series, percentile: float = 0.95):
    """The 'clean air' reference = a high percentile of recent gas resistance (the cleanest recent air).
    BSEC auto-calibrates this over days; we approximate it from stored history. Returns None with no data.
    Feed it a window (e.g. the last 24–48 h of gas_ohm) so the baseline tracks slow sensor drift."""
    vals = sorted(v for v in gas_ohm_series if v is not None and v > 0)
    if not vals:
        return None
    idx = min(len(vals) - 1, max(0, int(round(percentile * (len(vals) - 1)))))
    return vals[idx]
