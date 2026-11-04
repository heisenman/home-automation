#!/usr/bin/env python3
"""Dehumidifier health — condition-compensated drift alert (Hugh, 2026-11-03: catch a slow refrigerant leak, the
known failure of copper-coil whole-house units, months before the unit stops drying; study doc §7).

A dehumidifier's power and output move a lot with the air it's given, so a raw "L/kWh is down" says nothing. Every
indicator here is compared against what a baseline model predicts *for the same conditions*, and a run is only
scored when its conditions sit inside what the baseline has seen:

  indicator (one value per run)            leak / airflow fault shows as
  ─────────────────────────────────────────────────────────────────────────────
  w          compressor + fan power, W     lower  (less refrigerant → lower suction pressure → less work)
  pauses_h   compressor pauses per hour    higher (colder, starved coil frosts → more defrost pauses)
  lpkwh_sys  L/kWh incl. blower estimate   lower  (only once the HVAC-pair zero passes; readiness G2a)

  covariates: inlet temperature and dew point (the unit's inlet meter), line voltage, blower-on and heat-call share.

Method, per unit and indicator:
  1. Baseline = runs in the first BASELINE_DAYS after the start or the last MAINT event (≥ BASELINE_MIN_RUNS), frozen
     so a slow leak can't be absorbed into "normal".
  2. OLS on the covariates that actually vary in the baseline (at most one per 4 runs). The rest must match:
     a run whose covariates fall outside the baseline's range (± MARGIN) is *not scored* — no extrapolation.
     Nor is a run where the baseline is thin (leverage > LEV_MAX): the model's own error is shared by every run
     there, so scoring them would count one error many times (a season change read as a fault; simulated 2026-11-04).
  3. Unscored runs extend the baseline (new conditions, e.g. summer) only while the unit is healthy, so the model
     grows into new seasons without learning a fault.
  4. Score z = (observed − predicted) / prediction SE (leverage included). Two-sided CUSUM (k = 0.5, h = 4): the
     degraded side means drift; the other side means something changed (filter swap, sensor moved).
  5. Unit state: building baseline → ok → watch (one indicator drifted) → alert (two agree). Airflow faults (dirty
     filter, blocked duct) mimic low charge on power and pauses, so the alert says to check the filter first.

Runs piped on ha-2 with tools/dehum_readiness.py bundled ahead of it (reused: Data, ah, dp, held, runs_from, ols,
and readiness main() for the per-run L/kWh). 1-min rungs live only 7 days, so per-run observations are kept on .210
in instance/dehum-drift/runs.jsonl by tools/dehum_drift_report.sh, which passes them back in as _HISTORY_B64:

    tools/dehum_drift_report.sh            # daily from visko's crontab; files instance/dehum-drift/reports/
"""
import base64
import contextlib
import io
import json
import math
import sqlite3
import statistics as S
import sys
from datetime import datetime, timedelta, timezone

if not globals().get("_AS_LIBRARY"):          # run from the repo root on a box with the DBs: import the sibling
    sys.path.insert(0, "tools")
    from dehum_readiness import (APR_ON_W, HV_RET, MID_IN, PT, RUNGS, Data, dp, held, ols, pt, runs_from)
    from dehum_readiness import main as readiness_main
else:
    readiness_main = main                      # noqa: F821 — defined by the bundled dehum_readiness.py

UNITS = {
    # power meter, compressor-on threshold, inlet meter (what the unit breathes), uses the furnace blower
    "aprilaire": {"power": "dehum_pm", "on_w": APR_ON_W, "inlet": HV_RET},
    "midea": {"power": "plug_hall", "on_w": 150, "inlet": MID_IN},
}
INDICATORS = [("w", "power W", -1), ("pauses_h", "pauses / h", +1), ("lpkwh_sys", "L/kWh incl. blower", -1)]
# sign: −1 = a fault pushes it lower, +1 = higher. lo_cs/"degraded" accumulates movement in the fault direction.
COVARS = ["t_in", "dp_in", "v", "heat", "blower"]          # priority order when the baseline is small
MARGIN = {"t_in": 0.5, "dp_in": 0.5, "v": 1.5, "heat": 0.15, "blower": 0.15}
BASELINE_DAYS, BASELINE_MIN_RUNS = 21, 20
# Tuned on simulated runs (2026-11-04; 40 seeds per case, 90 runs, residual SD 2 W): leak of −0.5 W/run caught 40/40;
# healthy single-indicator false watch 1/40, across a +4 °C dew-point season change 2/40; two-indicator alert 0.
K, H = 0.5, 5.0
LEV_MAX = 0.08                 # score a run only where the baseline is dense (prediction SE ≤ ~1.04 × residual SD)
LOOKBACK_DAYS = 3                                         # re-measure recent runs each day (7-day rungs, daily cron)
WARMUP_MIN, PAUSE_GUARD_MIN = 20, 2
# Events that legitimately change a unit — filter change, service/recharge, relocation. Restart its baseline.
# (PT "YYYY-MM-DD HH:MM", unit, what)
MAINT = []


def pt_ep(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=PT).timestamp())


def observe(d, unit, cfg, a, b, apr_rows):
    """One observation per completed compressor run in [a, b)."""
    power = d.series(cfg["power"], "power_w", a, b)
    volt = d.series(cfg["power"], "voltage_v", a, b)
    inlet = d.th(cfg["inlet"], a, b)
    blower = d.series("hvac_blower", "running", a, b)
    hstate = d.series("thermostat_hall", "hvac_state", a, b)
    lp = {r["start"]: r["lpkwh_sys"] for r in apr_rows} if unit == "aprilaire" else {}
    out = []
    for s, e in runs_from(power, cfg["on_w"]):
        if e > b - 600:                                   # still running (or just ended): measure tomorrow
            continue
        on = {k: power.get(k, 0) > cfg["on_w"] for k in range(s, e, 60)}
        steady = [k for k in range(s + WARMUP_MIN * 60, e, 60) if held(on, k, PAUSE_GUARD_MIN)]
        if len(steady) < 20:
            continue
        cov = [k for k in steady if k in inlet]
        if len(cov) < 0.8 * len(steady):
            continue
        pauses = sum(1 for k in range(s + 60, e, 60) if on.get(k - 60) and not on.get(k))
        vv = [volt[k] for k in steady if k in volt]
        out.append({
            "unit": unit, "start": s, "end": e,
            "w": S.mean(power[k] for k in steady),
            "pauses_h": pauses / ((e - s) / 3600),
            "lpkwh_sys": lp.get(s),
            "t_in": S.mean(inlet[k][0] for k in cov),
            "dp_in": S.mean(dp(*inlet[k]) for k in cov),
            "v": S.mean(vv) if vv else None,
            "heat": S.mean(1.0 if hstate.get(k, 0) >= 0.5 else 0.0 for k in steady),
            "blower": S.mean(1.0 if blower.get(k, 0) >= 0.5 else 0.0 for k in steady),
        })
    return out


def fit_baseline(obs, ind):
    """OLS of one indicator on the covariates that vary in these runs. Returns a model dict or None."""
    rows = [o for o in obs if o.get(ind) is not None and all(o.get(c) is not None for c in ("t_in", "dp_in"))]
    if len(rows) < BASELINE_MIN_RUNS:
        return None
    use = []
    for c in COVARS:
        vals = [o[c] for o in rows if o.get(c) is not None]
        if len(vals) == len(rows) and S.pstdev(vals) > MARGIN[c] / 2 and len(rows) >= 4 * (len(use) + 2):
            use.append(c)
    X = [[1.0] + [o[c] for c in use] for o in rows]
    f = ols(X, [o[ind] for o in rows])
    if f is None:
        return None
    beta, se, inv, s2 = f
    rng = {c: (min(o[c] for o in rows if o.get(c) is not None), max(o[c] for o in rows if o.get(c) is not None))
           for c in COVARS if any(o.get(c) is not None for o in rows)}
    return {"use": use, "beta": beta, "se": se, "inv": inv, "s2": s2, "n": len(rows), "range": rng}


def in_range(m, o):
    for c, (lo, hi) in m["range"].items():
        if o.get(c) is None or not (lo - MARGIN[c] <= o[c] <= hi + MARGIN[c]):
            return False
    return True


def score(m, o, ind):
    """Prediction and z for one run; the SE includes the baseline fit's own uncertainty at this point (leverage)."""
    x = [1.0] + [o[c] for c in m["use"]]
    pred = sum(b * v for b, v in zip(m["beta"], x))
    lev = sum(x[i] * m["inv"][i][j] * x[j] for i in range(len(x)) for j in range(len(x)))
    if m["s2"] <= 0:
        return pred, 0.0, lev
    t = (o[ind] - pred) / math.sqrt(m["s2"] * (1 + lev))
    df = m["n"] - len(x)                                 # s2 from a small baseline makes t heavy-tailed: rescale it
    return pred, t * math.sqrt((df - 2) / df) if df > 2 else t, lev   # to unit variance before the CUSUM


def assess(obs, unit):
    """Walk this unit's runs in time order: frozen baseline, then score / extend. Returns per-indicator results."""
    obs = sorted((o for o in obs if o["unit"] == unit), key=lambda o: o["start"])
    starts = [pt_ep(t) for t, u, _ in MAINT if u == unit]
    epoch = max([s for s in starts if obs and s <= obs[-1]["start"]] or [obs[0]["start"] if obs else 0])
    obs = [o for o in obs if o["start"] >= epoch]
    res = {}
    for ind, label, sign in INDICATORS:
        have = [o for o in obs if o.get(ind) is not None]
        if not have:
            res[ind] = {"label": label, "state": "no data", "n": 0}
            continue
        t0 = have[0]["start"]
        base = [o for o in have if o["start"] < t0 + BASELINE_DAYS * 86400]
        later = [o for o in have if o["start"] >= t0 + BASELINE_DAYS * 86400]
        if len(base) < BASELINE_MIN_RUNS:                 # baseline still building: borrow later runs until enough
            base, later = have[:BASELINE_MIN_RUNS], have[BASELINE_MIN_RUNS:]
        m = fit_baseline(base, ind)
        if m is None:
            res[ind] = {"label": label, "state": f"building baseline ({len(have)}/{BASELINE_MIN_RUNS} runs)",
                        "n": len(have)}
            continue
        lo_cs = hi_cs = 0.0
        scored, skipped, extended, drift, drift_since = [], 0, 0, False, None
        for o in later:
            pred, z, lev = score(m, o, ind) if in_range(m, o) else (None, None, None)
            # Score only where the baseline is dense. A fitted model's error is shared by every run in a thinly
            # covered region, so scoring there would feed the CUSUM one error many times over (a season change
            # read as a fault). High-leverage runs are new conditions: learned while healthy, never judged.
            if lev is None or lev > LEV_MAX:
                if not drift:                             # new conditions while healthy: learn them
                    base.append(o)
                    m = fit_baseline(base, ind) or m
                    extended += 1
                else:
                    skipped += 1
                continue
            deg = sign * z                                # > 0 = worse than baseline (sign: -1 lower is worse)
            lo_cs = max(0.0, lo_cs + deg - K)
            hi_cs = max(0.0, hi_cs - deg - K)
            if lo_cs > H and not drift:
                drift_since = o["start"]
            drift = drift or lo_cs > H
            scored.append({"start": o["start"], "obs": o[ind], "pred": pred, "z": z})
        recent = [s["z"] for s in scored[-5:]]
        state = ("drift" if lo_cs > H else "changed" if hi_cs > H else
                 "watch" if len(recent) >= 3 and abs(S.mean(recent)) > 2 else "ok")
        res[ind] = {"label": label, "state": state, "n": len(have), "model": m, "scored": scored,
                    "skipped": skipped, "extended": extended, "cusum_degraded": lo_cs, "cusum_other": hi_cs,
                    "drift_since": drift_since}
    drifted = [r["label"] for r in res.values() if r["state"] == "drift"]
    if len(drifted) >= 2:
        verdict = ("alert", f"{' + '.join(drifted)} degraded together — check the filter and ducts first, then "
                            "refrigerant charge")
    elif drifted:
        verdict = ("watch", f"{drifted[0]} drifting on its own; needs a second indicator to confirm")
    elif any(r["state"] == "changed" for r in res.values()):
        verdict = ("changed", "an indicator moved the good way — log a MAINT event if something was serviced")
    elif all(r["state"].startswith(("building", "no data")) for r in res.values()):
        verdict = ("baseline", "building baseline")
    else:
        verdict = ("ok", "within baseline at matched conditions")
    return verdict, res, len(obs)


def drift_main():
    now = int(datetime.now(timezone.utc).timestamp()) // 60 * 60
    c = sqlite3.connect(f"file:{RUNGS}?mode=ro", uri=True)
    d = Data(c)
    with contextlib.redirect_stdout(io.StringIO()):
        r = readiness_main()                              # per-run L/kWh (system, incl. blower) + zero status
    apr_rows = r["runs"] if r and r.get("zero_ok") else []
    a = now - LOOKBACK_DAYS * 86400
    new = [o for u, cfg in UNITS.items() for o in observe(d, u, cfg, a, now, apr_rows)]
    hist = json.loads(base64.b64decode(globals().get("_HISTORY_B64", "")).decode() or "[]") \
        if globals().get("_HISTORY_B64") else []
    merged = {(o["unit"], o["start"]): o for o in hist}
    merged.update({(o["unit"], o["start"]): o for o in new})       # re-measured runs replace older values
    obs = list(merged.values())

    status = {"generated": pt(now).isoformat(), "units": {}}
    body = []
    for unit in UNITS:
        (state, why), res, n = assess(obs, unit)
        status["units"][unit] = {"state": state, "why": why, "runs": n,
                                 "indicators": {k: v["state"] for k, v in res.items()}}
        body.append(f"## {unit} — {state}: {why} ({n} runs)\n")
        for ind, label, sign in INDICATORS:
            x = res[ind]
            m = x.get("model")
            line = f"- **{label}**: {x['state']}"
            if m:
                terms = ", ".join(f"{c} {b:+.3g}" for c, b in zip(m["use"], m["beta"][1:])) or "constant"
                line += (f" — baseline {m['n']} runs, {terms}, residual SD {math.sqrt(m['s2']):.3g}; "
                         f"{len(x['scored'])} scored, {x['skipped']} outside range, {x['extended']} extended it; "
                         f"CUSUM degraded {x['cusum_degraded']:.1f} / other {x['cusum_other']:.1f} (h {H})"
                         + (f"; **drift since {pt(x['drift_since']):%Y-%m-%d %H:%M}**" if x["drift_since"] else ""))
            body.append(line)
        body.append("")
        mine = sorted((o for o in obs if o["unit"] == unit), key=lambda o: o["start"])[-10:]
        if mine:
            zs = {ind: {s["start"]: s for s in res[ind].get("scored", [])} for ind, _, _ in INDICATORS}
            body.append("| run (PT) | min | inlet °C / dp °C | V | heat / blower | W (z) | pauses/h (z) | L/kWh sys (z) |")
            body.append("|---|---|---|---|---|---|---|---|")
            for o in mine:
                def cell(ind, fmtv):
                    if o.get(ind) is None:
                        return "—"
                    s = zs[ind].get(o["start"])
                    return format(o[ind], fmtv) + (f" ({s['z']:+.1f})" if s else "")
                body.append(f"| {pt(o['start']):%m-%d %H:%M} | {(o['end'] - o['start']) // 60} | "
                            f"{o['t_in']:.1f} / {o['dp_in']:.1f} | {'—' if o['v'] is None else format(o['v'], '.1f')} | "
                            f"{o['heat']:.0%} / {o['blower']:.0%} | {cell('w', '.0f')} | {cell('pauses_h', '.2f')} | "
                            f"{cell('lpkwh_sys', '.2f')} |")
            body.append("")
    print(f"<!-- status: {json.dumps(status)} -->")
    print(f"<!-- runs: {json.dumps(new)} -->")
    print(f"# Dehumidifier health (condition-compensated drift) — {pt(now):%Y-%m-%d %H:%M %Z}\n")
    print("z = (observed − baseline prediction at the same inlet temperature, dew point, voltage, heat and blower "
          "share) / prediction SE. Runs outside the baseline's conditions aren't scored.\n")
    print("\n".join(body))
    if not apr_rows:
        print("Aprilaire L/kWh isn't scored until the HVAC-pair zero passes (readiness G2a).")


if __name__ == "__main__":
    drift_main()
