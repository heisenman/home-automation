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


# ── Unified air-quality index (ADR-0035) ─────────────────────────────────────────────
# One banded scale across three sensor families that do NOT measure the same thing. Each family maps its
# native signal onto a common 0..100 cleanliness score (higher = cleaner) whose band boundaries fall exactly
# at 80/60/40/20 → the 5 bands, and declares a `basis`: `absolute` (comparable room-to-room — only SGP30, via
# the UBA TVOC bands) or `relative` (only vs this room's own recent baseline — SGP40, BME680). Thresholds are
# the shadow-tuned ADR-0035 values. See docs/adr/ADR-0035 + docs/air-quality/SENSOR-METHODOLOGY.md.

# The 5-band scale (ADR-0035), cleanest→worst. Labels chosen so the ORDER is unambiguous (no two adjacent
# near-synonyms like the old Fair/Moderate). `band` int is the stable join key (client colors key off it, so
# relabeling never touches colors). band_legend() is the single source of truth — the UI renders its key from it.
_BANDS = ((80.0, 5, "Excellent"), (60.0, 4, "Good"), (40.0, 3, "Fair"), (20.0, 2, "Poor"), (0.0, 1, "Very Poor"))
_BAND_MEANING = {5: "clean air", 4: "good air", 3: "some pollutants — consider ventilating",
                 2: "poor — ventilate", 1: "very poor — ventilate now"}
_BAND_RANGE = {5: (80, 100), 4: (60, 80), 3: (40, 60), 2: (20, 40), 1: (0, 20)}


def band_for_score(score: float):
    """0..100 cleanliness score → (band_int 1..5, label). Boundaries at 80/60/40/20."""
    for lo, b, label in _BANDS:
        if score >= lo:
            return b, label
    return 1, "Very Poor"


def band_legend() -> list[dict]:
    """The band lookup table (single source of truth), cleanest→worst: each entry is
    {band, label, score_min, score_max, meaning}. The map renders its key from this so labels never drift."""
    return [{"band": b, "label": label, "score_min": _BAND_RANGE[b][0], "score_max": _BAND_RANGE[b][1],
             "meaning": _BAND_MEANING[b]} for _, b, label in _BANDS]


def _interp(x, xs, ys):
    """Piecewise-linear interpolation of x over knots xs (strictly increasing) → ys. Clamped at the ends."""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if x <= xs[i]:
            f = (x - xs[i - 1]) / (xs[i] - xs[i - 1])
            return ys[i - 1] + f * (ys[i] - ys[i - 1])
    return ys[-1]


# family → score knots (raw signal → cleanliness score). Boundaries land on band edges so score-band == raw-band.
import math as _math  # noqa: E402

# SGP30: UBA five-level TVOC bands (ppb), interpolated in LOG space (UBA is a log scale). basis=absolute.
_SGP30_LOGX = [_math.log(v) for v in (1.0, 65.0, 220.0, 660.0, 2200.0, 6000.0)]
_SGP30_Y = [100.0, 80.0, 60.0, 40.0, 20.0, 0.0]
# SGP40: Sensirion VOC Index native scale (100 = the sensor's own 24 h baseline). basis=relative.
_SGP40_X = [0.0, 100.0, 200.0, 300.0, 400.0, 500.0]
_SGP40_Y = [100.0, 80.0, 60.0, 40.0, 20.0, 0.0]
# SGP41 NOx Index. NOTE THE SCALE DIFFERENCE: unlike the VOC Index (100 = baseline), the NOx Index sits at
# 1 in clean air and only climbs on a NOx event (combustion — a gas hob, a nearby road, a wood stove).
# Sensirion's own guidance treats the range as 1..500 with 1 as "no NOx detected", so the knots below are
# NOT symmetric with the VOC ones and MUST NOT be copied from them.
#
# PROVISIONAL — these thresholds are NOT shadow-tuned. The SGP30/SGP40/BME680 knots above came out of the
# ADR-0035 characterization against weeks of this house's own data; we have no NOx history at all yet, so
# these are seeded from Sensirion's documented scale and are a starting point to be re-tuned once a real
# SGP41 has banked a few weeks. Recorded in docs/air-quality/SENSOR-METHODOLOGY.md.
_NOX_X = [1.0, 20.0, 50.0, 100.0, 200.0, 500.0]
_NOX_Y = [100.0, 80.0, 60.0, 40.0, 20.0, 0.0]
# BME680: gas-resistance ratio r = gas_ohm / clean-air baseline (pollution drops R). basis=relative.
_BME_X = [0.10, 0.30, 0.50, 0.70, 0.90, 1.00]
_BME_Y = [0.0, 20.0, 40.0, 60.0, 80.0, 100.0]


def _report(score, basis, family, raw_signal, raw_value, explanation, conf="ok", **extra):
    b, label = band_for_score(score)
    return {"air_quality": round(score, 1), "air_quality_band": b, "air_quality_band_label": label,
            "air_quality_basis": basis, "air_quality_conf": conf, "family": family,
            "raw_signal": raw_signal, "raw_value": raw_value, "explanation": explanation, **extra}


def _stale_report(family, raw_signal, conf, note):
    return {"air_quality": None, "air_quality_band": None, "air_quality_band_label": None,
            "air_quality_basis": None, "air_quality_conf": conf, "family": family,
            "raw_signal": raw_signal, "raw_value": None, "explanation": note}


def sgp30_air_quality(tvoc_ppb, eco2_ppm=None) -> dict:
    """SGP30 → ABSOLUTE band via UBA TVOC levels. eCO2 is a proxy-of-a-proxy → not banded (display only)."""
    if tvoc_ppb is None:
        return _stale_report("SGP30", "TVOC", "stale", "No TVOC reading.")
    if tvoc_ppb <= 0 and (eco2_ppm is None or eco2_ppm <= 400):
        return _stale_report("SGP30", "TVOC", "warmup", "SGP30 warming up (baseline not settled).")
    score = _interp(_math.log(max(tvoc_ppb, 1.0)), _SGP30_LOGX, _SGP30_Y)
    b, label = band_for_score(score)
    lvl = 6 - b   # UBA level 1 (best) .. 5 (worst)
    expl = (f"{label}. TVOC {tvoc_ppb:.0f} ppb (UBA level {lvl}) — an absolute, ethanol-referenced estimate, "
            f"comparable across rooms. Note: the SGP30's baseline drifts over days, so treat the absolute "
            f"level as approximate.")
    return _report(score, "absolute", "SGP30", "TVOC", round(tvoc_ppb, 0), expl, uba_level=lvl)


def sgp40_air_quality(voc_index) -> dict:
    """SGP40 → RELATIVE band on Sensirion's native VOC Index (100 = the sensor's own recent 24 h baseline)."""
    if voc_index is None:
        return _stale_report("SGP40", "VOC Index", "stale", "No VOC Index reading.")
    if voc_index <= 0:
        return _stale_report("SGP40", "VOC Index", "warmup", "SGP40 warming up (VOC Index not yet valid).")
    score = _interp(float(voc_index), _SGP40_X, _SGP40_Y)
    b, label = band_for_score(score)
    rel = ("at" if abs(voc_index - 100) < 5 else
           f"{voc_index - 100:.0f} above" if voc_index > 100 else f"{100 - voc_index:.0f} below")
    expl = (f"{label}. VOC Index {voc_index:.0f} — {rel} this room's recent (24 h) baseline of 100; "
            f"a relative measure, not comparable to other rooms.")
    return _report(score, "relative", "SGP40", "VOC Index", round(float(voc_index), 0), expl)


def sgp41_air_quality(voc_index, nox_index=None) -> dict:
    """SGP41 → RELATIVE band from TWO pixels: the SGP40's VOC Index plus a NOx Index.

    The two are not averaged. The band reports the WORSE of the two and names which pollutant drove it —
    the same dominant-pollutant rule a regulatory AQI uses, and the only safe one here: averaging would let
    clean VOC air mask a live combustion event, which is the specific thing the NOx pixel was added to
    catch. `nox_dominant` says which one won so the UI can label the cause.

    NOx lags: its algorithm needs ~4.75 h of learning against ~0.75 h for VOC (INIT_DURATION_MEAN_NOX in
    the Sensirion component), and the firmware holds it at 0 through the 10 s conditioning phase. So a
    fresh node reports a VOC-only band for the first few hours rather than a confidently wrong one.
    """
    voc = sgp40_air_quality(voc_index)
    voc["family"] = "SGP41"                        # same VOC pixel + algorithm as the SGP40
    if nox_index is None or nox_index <= 0:
        # No NOx yet (conditioning, blackout, or still learning) — band on VOC alone and say so.
        if voc.get("air_quality") is not None:
            voc["explanation"] += " NOx pixel still warming up — this band reflects VOC only."
        voc["nox_index"] = None
        voc["nox_dominant"] = False
        return voc
    nox_score = _interp(float(nox_index), _NOX_X, _NOX_Y)
    if voc.get("air_quality") is None:             # VOC not valid yet — band on NOx alone
        b, label = band_for_score(nox_score)
        expl = (f"{label}. NOx Index {nox_index:.0f} (1 = no NOx detected) — VOC pixel not yet valid, so "
                f"this band reflects NOx only.")
        return _report(nox_score, "relative", "SGP41", "NOx Index", round(float(nox_index), 0), expl,
                       nox_index=round(float(nox_index), 0), nox_dominant=True)
    if nox_score >= voc["air_quality"]:            # VOC is the worse (lower) score → keep the VOC report
        voc["explanation"] += f" NOx Index {nox_index:.0f} is clean (1 = no NOx detected)."
        voc["nox_index"] = round(float(nox_index), 0)
        voc["nox_dominant"] = False
        return voc
    b, label = band_for_score(nox_score)
    expl = (f"{label}. NOx Index {nox_index:.0f} — elevated above the clean-air value of 1, which points at "
            f"a combustion source (gas hob, stove, traffic). This is the worse of the two pixels; VOC Index "
            f"is {voc_index:.0f}. A relative measure, not comparable to other rooms.")
    return _report(nox_score, "relative", "SGP41", "NOx Index", round(float(nox_index), 0), expl,
                   nox_index=round(float(nox_index), 0), voc_index=round(float(voc_index), 0),
                   nox_dominant=True)


def bme680_air_quality(gas_ohm, ambient_rh_pct, gas_baseline_ohm, gas_valid=1, baseline_ready=True) -> dict:
    """BME680 → RELATIVE band on the resistance ratio r = gas_ohm / clean-air baseline (de-clamped; the old
    0..100 index pinned ~22 % of samples at 100). Humidity-compensates gas_ohm with the reference sensor's
    TRUE ambient RH before the ratio, so the band reflects VOC load, not humidity artifacts."""
    if gas_ohm is None:
        return _stale_report("BME680", "gas resistance", "stale", "No gas-resistance reading.")
    if not gas_valid:
        return _stale_report("BME680", "gas resistance", "stale", "BME680 gas reading not valid (heater unstable).")
    if not gas_baseline_ohm or gas_baseline_ohm <= 0:
        return _stale_report("BME680", "gas resistance", "burn_in", "BME680 clean-air baseline still forming.")
    if not baseline_ready:
        return _stale_report("BME680", "gas resistance", "burn_in", "BME680 clean-air baseline still forming.")
    if ambient_rh_pct is None:
        # no co-located reference sensor → we can't remove the humidity artifact; don't fabricate an index.
        return _stale_report("BME680", "gas resistance", "no_ref",
                             "No co-located reference humidity sensor — can't compensate the reading.")
    # humidity correction: normalize gas_ohm toward the HUM_REF_PCT reference (MOX R rises with dryness);
    # scale by the humidity_score fraction so very dry/humid air doesn't masquerade as a VOC change.
    hs = humidity_score(ambient_rh_pct) / (HUM_WEIGHT * 100.0)       # 0..1, full marks near 40 %RH
    g_eff = gas_ohm * (0.85 + 0.15 * hs)                             # gentle correction (±15 %)
    r = g_eff / gas_baseline_ohm
    score = _interp(r, _BME_X, _BME_Y)
    b, label = band_for_score(score)
    expl = (f"{label}. Gas resistance {gas_ohm / 1000:.0f} kΩ at {min(r, 1.0) * 100:.0f}% of this room's "
            f"recent clean-air baseline; a relative measure, not comparable to other rooms.")
    return _report(score, "relative", "BME680", "gas resistance", round(gas_ohm, 0), expl,
                   gas_baseline_ohm=round(gas_baseline_ohm, 0), ratio=round(r, 3))


def air_quality_for(device_type, metrics, *, ambient_rh_pct=None, gas_baseline_ohm=None,
                    baseline_ready=True) -> dict | None:
    """Dispatch a gas node to its family's unified air-quality report (ADR-0035). Returns None if the
    device_type isn't a known gas family. `metrics` is the node's latest {metric: value}."""
    dt = (device_type or "").lower()
    m = metrics or {}
    if "sgp30" in dt:
        return sgp30_air_quality(m.get("tvoc"), m.get("eco2"))
    if "sgp41" in dt:                       # before sgp40: the SGP41 carries a VOC pixel too
        return sgp41_air_quality(m.get("voc_index"), m.get("nox_index"))
    if "sgp40" in dt:
        return sgp40_air_quality(m.get("voc_index"))
    if "bme680" in dt:
        return bme680_air_quality(m.get("gas_ohm"), ambient_rh_pct, gas_baseline_ohm,
                                  m.get("gas_valid", 1), baseline_ready)
    return None
