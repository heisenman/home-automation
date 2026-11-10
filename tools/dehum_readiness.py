#!/usr/bin/env python3
"""Dehumidifier study — readiness gates + daily statistics (docs/design/dehumidifier-efficiency-study.md §6c).

Read-only on ha-2's rungs.db / control.db. Prints a markdown report whose first line is an HTML comment holding the
machine-readable status (`<!-- status: {...} -->`), so the schedule can be tracked without anyone reading the prose.
Stdlib only, so it runs piped over ssh like the Phase 0/1 tools (helpers are duplicated from dehum_phase1.py on
purpose: a piped script can't import its siblings on ha-2):

    ssh 192.168.1.200 'cd ~/home_automation && python3 -' < tools/dehum_readiness.py

Filed daily on .210 by tools/dehum_readiness_report.sh (cron), under instance/dehum-study/reports/.
"""
import json
import math
import sqlite3
import statistics as S
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
RUNGS, CONTROL, WEATHER = "instance/db/rungs.db", "instance/db/control.db", "instance/db/weather.db"
RHO = 1.2                                       # kg/m3, air near 21 °C
CFM = 1.699                                     # m3/h per cfm

# Facts only Hugh can supply. Edit + commit when he answers (the report lists what is still missing).
MANUAL = {
    # continuous-fan (G) tap 1-5. Hugh 2026-11-03: factory wiring, red = motor tap 1 on LO HT; fan-only is taken to
    # run at the LO HT speed (this board has no separate fan terminal; confirm in the installation manual)
    "fan_tap": 1,
    "furnace_venting": "two-pipe",   # Hugh 2026-11-03: both PVC pipes run outside (photo); combustion air is outdoor
    "static_in_wc": 0.3,        # assumed external static for the tap table (§2); refine if measured
}
# 59TP5A060 air delivery at 0.1 / 0.3 / 0.5 in. w.c. (59TP5A-02PD, §2)
TAP_CFM = {1: (785, 655, 530), 2: (1160, 1080, 1000), 3: (1010, 925, 835), 4: (1325, 1255, 1185),
           5: (1720, 1620, 1505)}

HEAT_CFM = {1: 780, 2: 1000}                   # rated heating airflow low/high stage @ 0.12 in. (§2)
# Furnace blower at the fan tap, while it runs for the Aprilaire (no backdraft damper, so it must). Not metered:
# estimate for the X13 at tap 1, ~100-200 W (study doc §7, 2026-11-03). Counted in the Aprilaire's system L/kWh.
BLOWER_FAN_W = 150

ROOMS = ["meter_pro_living_room", "meter_kitchen", "meter_c_bed", "meter_pro_c_office", "meter_pro_h_bed"]
OUTDOOR, CRAWL, LR = "switchbot_outdoor", "ometer_crawlspace", "meter_pro_living_room"
MID_IN, MID_OUT = "study_midea_inlet", "study_midea_outlet"
HV_RET, HV_SUP, APR_DIS = "study_hvac_return", "study_hvac_supply", "study_apr_discharge"   # APR_DIS optional
SETTLE_MIN = 60                                 # new meters read wrong while they settle (Hugh)
APR_ON_W, APR_IDLE_W = 300, 30                  # dehum_pm: compressor ~570 W, its fan 87 W, standby ~3 W
STATE_SETTLE_MIN = 10                           # minutes a state must hold before its minutes count

# Schedule (§6c). Phase 3 checks repeat weekly from its first date until the gate passes.
DUE = {"G2a": date(2026, 11, 3), "G2b": date(2026, 11, 8), "P3": date(2026, 11, 15)}
P3_ARMS = ["0", "M", "A", "MA", "E"]
DP_BINS = [(-99, 7.5), (7.5, 9.5), (9.5, 99)]   # inlet dew point °C bins for coverage
DP_REF = 8.0                                    # °C: G2b's efficiency-vs-inlet-dew-point line is quoted here


def ah(t, rh):
    return 6.112 * math.exp(17.67 * t / (t + 243.5)) * rh * 2.1674 / (273.15 + t)


def dp(t, rh):
    g = math.log(max(rh, 0.1) / 100) + 17.62 * t / (243.12 + t)
    return 243.12 * g / (17.62 - g)


def pt(ts):
    return datetime.fromtimestamp(ts, PT)


class Data:
    def __init__(self, c):
        self.c = c

    def series(self, dev, metric, a, b, res="1min"):
        return dict(self.c.execute("SELECT bucket_start, vmean FROM rung WHERE res=? AND device_id=? AND metric=? "
                                   "AND bucket_start>=? AND bucket_start<?", (res, dev, metric, a, b)))

    def first_seen(self, dev):
        r = self.c.execute("SELECT min(bucket_start) FROM rung WHERE res='1min' AND device_id=? "
                           "AND metric='humidity_pct'", (dev,)).fetchone()
        return r[0]

    def ah_series(self, dev, a, b):
        t, h = self.series(dev, "temperature_c", a, b), self.series(dev, "humidity_pct", a, b)
        return {k: ah(t[k], h[k]) for k in t if k in h}

    def th(self, dev, a, b):
        t, h = self.series(dev, "temperature_c", a, b), self.series(dev, "humidity_pct", a, b)
        return {k: (t[k], h[k]) for k in t if k in h}


# Outdoor cross-check, the ERV dry boost's rule (server/control/dry_boost.py outdoor_dewpoint, duplicated: a piped
# script can't import it). switchbot_outdoor reads RH high when wet (dew point +1.4 °C median, +2.2 at ≥ 95 % RH vs
# Open-Meteo, FOLLOWUPS), which inflated the ERV moisture term in the Aprilaire's ΔAH (Hugh, 2026-11-09).
AGREE_C, WET_RH, REF_STALE_S = 1.5, 80.0, 45 * 60


def openmeteo_th(a, b):
    """Open-Meteo current conditions {epoch: (t, rh)} from weather.db, or {} if it can't be read."""
    try:
        w = sqlite3.connect(f"file:{WEATHER}?mode=ro", uri=True)
        rows = w.execute("SELECT ts, metric, value FROM weather WHERE source='openmeteo' AND ts>=? AND ts<? AND "
                         "metric IN ('temperature_c','humidity_pct')",
                         (datetime.fromtimestamp(a - REF_STALE_S, timezone.utc).strftime("%Y-%m-%dT%H:%M"),
                          datetime.fromtimestamp(b, timezone.utc).strftime("%Y-%m-%dT%H:%M"))).fetchall()
    except sqlite3.Error:
        return {}
    by = {}
    for ts, m, v in rows:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        k = int((t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp())
        by.setdefault(k, {})[m] = v
    return {k: (v["temperature_c"], v["humidity_pct"]) for k, v in by.items() if len(v) == 2}


def outdoor_ah(d, a, b):
    """Per-minute outdoor AH: the sensor, unless Open-Meteo disagrees by more than AGREE_C of dew point — then
    Open-Meteo when the sensor is the wetter one at RH ≥ WET_RH (its wet bias), else the wetter of the two.
    Open-Meteo older than REF_STALE_S: sensor only."""
    sens, ref = d.th(OUTDOOR, a, b), openmeteo_th(a, b)
    rk, j, out = sorted(ref), 0, {}
    for k in sorted(sens):
        t, h = sens[k]
        while j + 1 < len(rk) and rk[j + 1] <= k:
            j += 1
        if not rk or rk[j] > k or k - rk[j] > REF_STALE_S:
            out[k] = ah(t, h)
            continue
        rt, rh = ref[rk[j]]
        ds, dr = dp(t, h), dp(rt, rh)
        if abs(ds - dr) <= AGREE_C:
            out[k] = ah(t, h)
        elif ds > dr and h >= WET_RH:
            out[k] = ah(rt, rh)
        else:
            out[k] = ah(t, h) if ds >= dr else ah(rt, rh)
    return out


def held(flags, k, n=STATE_SETTLE_MIN):
    """True if flags (minute -> bool) was True for the n minutes up to and including k."""
    return all(flags.get(k - 60 * i, False) for i in range(n + 1))


def pair_delta(d, dev_in, dev_out, minutes, a, b):
    """Per-minute outlet - inlet AH over the given minutes (after both meters settled)."""
    fi, fo = d.first_seen(dev_in), d.first_seen(dev_out)
    if fi is None or fo is None:
        return None
    start = max(fi, fo) + SETTLE_MIN * 60
    wi, wo = d.ah_series(dev_in, max(a, start), b), d.ah_series(dev_out, max(a, start), b)
    return {k: wo[k] - wi[k] for k in minutes if k in wi and k in wo}


def chunk_means(delta, max_min=60, min_min=5):
    """Means of contiguous stretches of minutes, cut at max_min, keeping those >= min_min. Heat cycles give short
    blower-only stretches, so clock-hour means would throw most of them away."""
    out, cur, last = [], [], None
    for k in sorted(delta):
        if cur and (k - last > 60 or len(cur) >= max_min):
            out.append(cur)
            cur = []
        cur.append(delta[k])
        last = k
    if cur:
        out.append(cur)
    return [(len(c), S.mean(c)) for c in out if len(c) >= min_min]


def offset_stats(delta):
    ch = chunk_means(delta)
    if len(ch) < 3:
        return None
    w = [n for n, _ in ch]
    m = sum(n * v for n, v in ch) / sum(w)
    sd = S.stdev(v for _, v in ch)
    days = {pt(k).date() for k in delta}
    return {"hours": sum(w) / 60, "chunks": len(ch), "days": len(days), "mean": m, "sd": sd,
            "se": sd / math.sqrt(len(ch))}


def drift_test(days, sd):
    """Do daily offset means differ by more than their own SEs? χ² of the means about their inverse-variance
    weighted mean, df = days − 1; p from the Wilson–Hilferty approximation. Returns (χ², p). Each day's SE uses
    the pooled chunk SD (sd) over its chunk count: a short day's own SD from a few chunks can be absurdly small
    (11-04: ±0.01 from 5 chunks) and would dominate the weights."""
    w = [x["chunks"] / sd ** 2 for x in days]
    m = sum(wi * x["mean"] for wi, x in zip(w, days)) / sum(w)
    chi2 = sum(wi * (x["mean"] - m) ** 2 for wi, x in zip(w, days))
    df = len(days) - 1
    z = ((chi2 / df) ** (1 / 3) - (1 - 2 / (9 * df))) / math.sqrt(2 / (9 * df))
    return chi2, 0.5 * math.erfc(z / math.sqrt(2))


def ols(X, y):
    """Least squares with standard errors (normal equations + Gauss-Jordan; no numpy on ha-2). As dehum_phase1."""
    n, p = len(X), len(X[0])
    A = [[sum(X[r][i] * X[r][j] for r in range(n)) for j in range(p)] for i in range(p)]
    bv = [sum(X[r][i] * y[r] for r in range(n)) for i in range(p)]
    M = [A[i][:] + [1.0 if i == j else 0.0 for j in range(p)] for i in range(p)]
    for col in range(p):
        piv = max(range(col, p), key=lambda r: abs(M[r][col]))
        M[col], M[piv] = M[piv], M[col]
        dd = M[col][col]
        if abs(dd) < 1e-12:
            return None
        M[col] = [v / dd for v in M[col]]
        for r in range(p):
            if r != col:
                f = M[r][col]
                M[r] = [vr - f * vc for vr, vc in zip(M[r], M[col])]
    inv = [row[p:] for row in M]
    beta = [sum(inv[i][j] * bv[j] for j in range(p)) for i in range(p)]
    res = [y[r] - sum(beta[i] * X[r][i] for i in range(p)) for r in range(n)]
    s2 = sum(e * e for e in res) / max(n - p, 1)
    return beta, [math.sqrt(max(inv[i][i] * s2, 0)) for i in range(p)], inv, s2


T_REF = 21.5                                    # °C: the fan-only offset is quoted at this return temperature


def zero_model(delta, state, x, cfm, tret=None):
    """Fit the HVAC pair's no-Aprilaire behaviour (study doc §6b, duct topology). With the Aprilaire idle, supply −
    return AH = sensor offset + the ERV's fresh air mixed in:
        blower off (S0): the loop carries ERV air only        → off_S0 + b0·x
        blower on, fan (S1f) / heat speed (S1h): diluted      → off_S1 + g·cfm·x/100
    x = W_out − W_return (g/m³). b0 = mixing at the register × (1 − ERV latent effectiveness); g = 100(1 − ε)/Q_blower.
    The sensor offset is PER BLOWER STATE (Hugh approved 2026-11-06): airflow over the pair shifts it (the Midea pair
    −0.01 off vs +0.12 on), and one shared offset let 48 h of blower-off data set the zero for blower-on runs.
    beta = [off_S1f, b0, g, g_h, off_S0, off_S1h, k_t]; beta[0] + beta[2] is what an Aprilaire run (fan speed) uses.
    k_t (fan-only only, needs tret = return-meter °C): the pair's offset rises with return temperature, +0.097 ±
    0.025 g/m³ per °C on the full archive (Hugh 2026-11-09, tools/dehum_erv_archive_fit.py); beta[0] is the offset
    at T_REF.
    Fitted on contiguous same-state stretches (chunk means). Returns {"beta", "se", "n", "hours"} or None."""
    rows = []
    for st in ("S0", "S1f", "S1h"):
        sub = {k: v for k, v in delta.items() if state.get(k) == st and k in x and k in cfm}
        cur, last = [], None
        for k in sorted(sub):
            if cur and (k - last > 60 or len(cur) >= 60):
                rows.append((st, cur))
                cur = []
            cur.append(k)
            last = k
        if cur:
            rows.append((st, cur))
    rows = [(st, ks) for st, ks in rows if len(ks) >= 5]
    if len(rows) < 6 or sum(1 for st, _ in rows if st == "S1f") < 3:
        return None                                  # the run zero (S1f offset + g) needs fan-only stretches
    # The states share no parameter, so each is its own fit with its own residual variance: pooling them let the
    # noisier blower-off stretches inflate the fan-only SE 2x (0.132 vs 0.063 on 2026-11-06).
    beta, se, s1f = [0.0] * 7, [None] * 7, []
    slots = {"S1f": (0, 2), "S0": (4, 1), "S1h": (5, 3)}          # (offset index, ERV-term index)
    for st, (io, ix) in slots.items():
        sub = [ks for s2, ks in rows if s2 == st]
        if not sub:
            continue
        xs = [S.mean(x[k] for k in ks) if st == "S0" else S.mean(cfm[k] * x[k] / 100 for k in ks) for ks in sub]
        ys = [S.mean(delta[k] for k in ks) for ks in sub]
        ts = ([S.mean(tret[k] for k in ks if k in tret) - T_REF if any(k in tret for k in ks) else None for ks in sub]
              if st == "S1f" and tret else None)
        use_t = ts is not None and None not in ts and len(sub) >= 6
        fit = (ols([[1.0, xv, tv] for xv, tv in zip(xs, ts)], ys) if use_t else
               ols([[1.0, xv] for xv in xs], ys) if len(sub) >= 3 else None)
        if fit is None:
            if st == "S1f":
                return None
            continue
        beta[io], beta[ix] = fit[0][:2]
        se[io], se[ix] = fit[1][:2]
        if use_t:
            beta[6], se[6] = fit[0][2], fit[1][2]
        if st == "S1f":                              # per-chunk residual offsets, for the day-to-day drift test
            s1f = [(ks[0], len(ks), yv - beta[ix] * xv - (beta[6] * tv if use_t else 0.0))
                   for ks, xv, yv, tv in zip(sub, xs, ys, ts or [0.0] * len(sub))]
    hours = {st: sum(len(ks) for s2, ks in rows if s2 == st) / 60 for st in ("S0", "S1f", "S1h")}
    days = {pt(k).date() for _, ks in rows for k in ks}
    return {"beta": beta, "se": se, "n": len(rows), "hours": hours, "days": len(days), "s1f": s1f}


SEG_MIN, SEG_TAIL_MIN = 240, 60                 # G2b scores long calls in 4-h segments (Hugh 2026-11-09)


def segments(runs, seg_min=SEG_MIN, tail_min=SEG_TAIL_MIN):
    """Split each call into seg_min pieces -> [(start, end, first_of_call)]. A tail shorter than tail_min joins the
    piece before it. At 44/40 the block's Aprilaire can run for days as ONE call, which would give G2b's
    efficiency-vs-inlet-dew-point line a single point; segments give it one per 4 h across the day's dew points."""
    out = []
    for s, e in runs:
        cuts = list(range(s, e, seg_min * 60)) + [e]
        if len(cuts) > 2 and cuts[-1] - cuts[-2] < tail_min * 60:
            del cuts[-2]
        out += [(a, b, i == 0) for i, (a, b) in enumerate(zip(cuts, cuts[1:]))]
    return out


def runs_from(power, on_w, gap_min=6, min_len=30):
    """Contiguous minutes with power > on_w -> [(start, end_exclusive)]. Gaps <= gap_min are bridged: the E070
    cycles its compressor off ~4 min every hour within one call, and a run here means one call."""
    on = sorted(k for k, v in power.items() if v > on_w)
    out, s, last = [], None, None
    for k in on:
        if s is None:
            s = k
        elif k - last > gap_min * 60:
            out.append((s, last + 60))
            s = k
        last = k
    if s is not None:
        out.append((s, last + 60))
    return [(x, y) for x, y in out if y - x >= min_len * 60]


def crit(name, ok, detail):
    return {"name": name, "ok": ok, "detail": detail}


# Last filed report's verdicts ({"hvac_zero": bool, "midea_zero": bool}), prepended by dehum_readiness_report.sh.
# Empty when run by hand: then there is no hysteresis, only the target and the preponderance path.
PREV_OK = globals().get("_PREV_OK") or {}
HOLD_X = 1.3                 # a passed SE criterion stays passed until SE exceeds HOLD_X × target


def se_gate(key, se, target, prepond=None):
    """An SE criterion that isn't a hard wall (Hugh 2026-11-06: "allow hysteresis and a preponderance of data to
    eventually override your 0.05 hard number"). (ok, how):
      SE ≤ target                                    -> pass;
      passed in the last report and SE ≤ HOLD_X·target -> stays passed (hysteresis: noise can't flip it back);
      `prepond` (a reason string)                    -> pass on the weight of evidence: enough data, no drift,
                                                        SE under a looser ceiling (the caller decides)."""
    if se is None:
        return False, ""
    if se <= target:
        return True, ""
    if PREV_OK.get(key) and se <= HOLD_X * target:
        return True, f" — held: passed before, SE within {HOLD_X:g}× target"
    if prepond:
        return True, f" — preponderance of data: {prepond}"
    return False, ""


def day_drift(chunks, sd):
    """Day-to-day drift test over [(minute key, n, value)] chunks: (chi2, p, days) or None (drift_test per day)."""
    by = {}
    for k, n, v in chunks:
        by.setdefault(pt(k).date(), []).append(v)
    days = [{"mean": S.mean(v), "chunks": len(v)} for v in by.values()]
    if len(days) < 2 or not sd:
        return None
    chi2, p = drift_test(days, sd)
    return chi2, p, len(days)


def main():
    now = int(datetime.now(timezone.utc).timestamp()) // 60 * 60
    today = pt(now).date()
    c = sqlite3.connect(f"file:{RUNGS}?mode=ro", uri=True)
    d = Data(c)
    study_a = int(datetime(2026, 10, 31, 16, 30, tzinfo=PT).timestamp())   # Midea off at the plug 16:21 PDT
    day_a = now - 86400

    apr = d.series("dehum_pm", "power_w", study_a, now)
    blower = d.series("hvac_blower", "running", study_a, now)
    hstate = d.series("thermostat_hall", "hvac_state", study_a, now)    # Venstar state: 0 idle, 1 heat, 2 cool
    stage = d.series("thermostat_hall", "active_stage", study_a, now)
    mid_p = d.series("plug_hall", "power_w", study_a, now)
    apr_runs = runs_from(apr, APR_ON_W)
    tap = MANUAL["fan_tap"]
    q_fan = (TAP_CFM[tap][[0.1, 0.3, 0.5].index(MANUAL["static_in_wc"])] * CFM) if tap else None   # m3/h

    gates = []

    # ---- G2a: HVAC pair zeroed + manual facts --------------------------------------------------------------
    hv_seen = [d.first_seen(x) for x in (HV_RET, HV_SUP)]
    reg = all(x is not None for x in hv_seen)
    fresh = reg and all(d.series(x, "humidity_pct", now - 6 * 3600, now) for x in (HV_RET, HV_SUP))
    apr_idle = {k: v < APR_IDLE_W for k, v in apr.items()}
    blower_on = {k: v >= 0.99 for k, v in blower.items()}
    blower_off = {k: v <= 0.01 for k, v in blower.items()}
    # heat cycles are short (10-20 min), so the blower only has to hold 3 min; the Aprilaire must have been idle
    # 15 min so its last wet/dry air has cleared the ducts; blower-off needs 10 min for the loop to hold ERV air only
    # Heat calls are excluded, with 25 min after each: the supply meter reads +1.6-1.9 g/m3 during a call and
    # drifts back as its body cools (2026-11-02 03:16 cycle) — a sensor artifact at 30-37 °C / 20-30 % RH, since
    # real combustion water would need the whole flame's output in the supply air.
    no_heat = {k: hstate.get(k, 0) < 0.5 for k in blower}
    state = {}
    for k in blower:
        if not (held(apr_idle, k, 15) and held(no_heat, k, 25)):
            continue
        if held(blower_off, k, 10):
            state[k] = "S0"
        elif held(blower_on, k, 3):
            state[k] = "S1f"
    zm = None
    erv_cfm = d.series("erv_attic", "supply_cfm", study_a, now)
    w_out = outdoor_ah(d, study_a - 1800, now)       # cross-checked against Open-Meteo
    if reg:
        w_ret = d.ah_series(HV_RET, study_a, now)
        hv_delta = pair_delta(d, HV_RET, HV_SUP, list(blower), study_a, now) or {}
        okeys = sorted(w_out)
        xdiff, j = {}, 0
        for k in sorted(w_ret):                      # outdoor meter reports less often: last value within 15 min
            while j + 1 < len(okeys) and okeys[j + 1] <= k:
                j += 1
            if okeys and okeys[j] <= k and k - okeys[j] <= 900:
                xdiff[k] = w_out[okeys[j]] - w_ret[k]
        t_ret = d.series(HV_RET, "temperature_c", study_a, now)
        zm = zero_model({k: v for k, v in hv_delta.items() if k in state}, state, xdiff, erv_cfm, t_ret)
    hz = {"mean": zm["beta"][0], "se": zm["se"][0]} if zm else None
    mid_idle = {k: mid_p.get(k, 999) < 5 for k in mid_p}
    # blower-off only: with the blower on, supply air reaches the pair unevenly (outlet − inlet +1.2-1.7 g/m3 on
    # 2026-11-01 21-01 h with the Midea off), so its zero is per blower state; the blower-on zero is reported apart
    mid_ok = [k for k in mid_idle if held(mid_idle, k, 60)]
    mid_d = pair_delta(d, MID_IN, MID_OUT, [k for k in mid_ok if held(blower_off, k, 10)], study_a, now) or {}
    mz = offset_stats(mid_d)
    by_day = {}
    for k, v in mid_d.items():
        by_day.setdefault(pt(k).date(), {})[k] = v
    # day-to-day drift judged against each day's own SE (Hugh agreed 2026-11-04): a fixed 0.15 g/m³ range was
    # tighter than the noise (day SEs 0.06-0.18), so it would only ever pass by luck
    mid_days = {dd: st for dd, sub in by_day.items() if len(sub) >= 180 and (st := offset_stats(sub))}  # ≥ 3 h days
    mid_drift = drift_test(list(mid_days.values()), mz["sd"]) if len(mid_days) >= 2 and mz else None
    mzb = offset_stats(pair_delta(d, MID_IN, MID_OUT, [k for k in mid_ok if held(blower_on, k, 10)], study_a, now)
                       or {})
    # SE gates with hysteresis + a preponderance path (se_gate). HVAC: the fan-only offset after ≥ 6 h / 12 stretches
    # over ≥ 4 days, no day-to-day drift beyond the stretches' own scatter, SE ≤ 2× target.
    hv_ok, hv_how, hv_dr = False, "", None
    if zm and zm["hours"]["S1f"] >= 2 and zm["hours"]["S0"] >= 3 and zm["days"] >= 2 and zm["se"][0] is not None:
        res = [v for _, _, v in zm["s1f"]]
        hv_dr = day_drift(zm["s1f"], S.stdev(res) if len(res) >= 2 else None)
        pre = (f"{zm['hours']['S1f']:.1f} h fan-only in {len(res)} stretches over {hv_dr[2]} days, no drift "
               f"(p {hv_dr[1]:.2f}), SE ≤ {2 * 0.05:.2f}"
               if hv_dr and zm["hours"]["S1f"] >= 6 and len(res) >= 12 and hv_dr[2] >= 4 and hv_dr[1] >= 0.05
               and zm["se"][0] <= 2 * 0.05 else None)
        hv_ok, hv_how = se_gate("hvac_zero", zm["se"][0], 0.05, pre)
    mid_ok_base = bool(mz and mz["hours"] >= 12 and mid_drift is not None and mid_drift[1] >= 0.05)
    md_ok, md_how = se_gate("midea_zero", mz["se"] if mid_ok_base else None, 0.08,
                            f"{mz['hours']:.0f} h over {len(mid_days)} days, SE ≤ 0.16"
                            if mid_ok_base and mz["hours"] >= 36 and len(mid_days) >= 4 and mz["se"] <= 0.16 else None)
    g2a = [
        crit("HVAC pair registered and reporting", fresh,
             "both reporting" if fresh else ("registered, not reporting in 6 h" if reg else
                                             f"not registered yet ({HV_RET}, {HV_SUP})")),
        crit("HVAC pair zero model, offset per blower state: ≥ 2 h fan-only and ≥ 3 h blower-off (Aprilaire idle, "
             "no heat) over ≥ 2 days, fan-only offset SE ≤ 0.05 g/m³ (or held / preponderance of data)",
             hv_ok,
             (f"fan-only offset {zm['beta'][0]:+.3f} ± {zm['se'][0]:.3f} g/m³, ERV g {zm['beta'][2]:+.3f} ± "
              f"{zm['se'][2] or 0:.3f} (per 100 cfm·g/m³; physics ≈ 0.15·(1 − ε))"
              + (f", temperature {zm['beta'][6]:+.3f} ± {zm['se'][6]:.3f} per °C (offset at {T_REF} °C)"
                 if zm["se"][6] is not None else "")
              + f"; blower-off offset "
              f"{zm['beta'][4]:+.3f} ± {zm['se'][4] or 0:.3f}, b0 {zm['beta'][1]:+.2f}; hours blower-off/fan-only "
              f"{zm['hours']['S0']:.1f}/{zm['hours']['S1f']:.1f} over {zm['days']} d"
              + (f"; fan-only day drift χ² {hv_dr[0]:.1f}, p {hv_dr[1]:.2f} over {hv_dr[2]} d" if hv_dr else "")
              + hv_how)
             if zm else (f"{sum(1 for v in state.values() if v != 'S0')} blower-on and "
                         f"{sum(1 for v in state.values() if v == 'S0')} blower-off Aprilaire-idle minutes; "
                         + ("not enough paired stretches to fit yet" if reg else "no paired meter data"))),
        crit("Continuous-fan tap known", tap is not None, f"tap {tap} ≈ {q_fan / CFM:.0f} cfm" if tap else
             "read the tap the G (fan) terminal uses off the 59TP5 control board"),
        crit("Furnace venting known", MANUAL["furnace_venting"] is not None,
             MANUAL["furnace_venting"] or "two-pipe or one-pipe? (two PVC pipes outside, or intake open in closet)"),
        crit("Midea pair zero offset, blower off (≥ 12 idle h over ≥ 2 days, no day-to-day drift beyond the days' "
             "own SE at p < 0.05, offset SE ≤ 0.08 g/m³, or held / preponderance of data)",
             md_ok,
             (f"{mz['hours']:.1f} h, offset {mz['mean']:+.3f} ± {mz['se']:.3f} g/m³ (hourly SD {mz['sd']:.3f})"
              if mz else "not enough idle hours")
             + (f"; daily means " + ", ".join(f"{dd:%m-%d} {v['mean']:+.2f} ± {mz['sd'] / math.sqrt(v['chunks']):.2f}"
                                             for dd, v in sorted(mid_days.items()))
                + (f" (drift χ² {mid_drift[0]:.1f}, p {mid_drift[1]:.2f})" if mid_drift else "")
                if mid_days else "")
             + (f"; blower on: {mzb['mean']:+.3f} ± {mzb['se']:.3f} over {mzb['hours']:.1f} h" if mzb else "")
             + md_how),
    ]
    gates.append(("G2a", "HVAC meters zeroed", g2a))

    # ---- G2b: Aprilaire measured across the air handler ---------------------------------------------------
    run_rows = []
    ret_th = d.th(HV_RET, study_a, now) if reg else {}
    for s, e, first in segments(apr_runs):
        ms, me = (s + 20 * 60 if first else s), e    # skip coil wetting / mixing (first segment of a call only)
        if me - ms < 20 * 60:
            continue
        kwh = sum(apr.get(k, 0) for k in range(s, e, 60)) / 60 / 1000
        mins = [k for k in range(ms, me, 60) if blower.get(k, 0) >= 0.99 and held(no_heat, k, 25)]
        dl = pair_delta(d, HV_RET, HV_SUP, mins, ms, me) if reg else None
        row = {"start": s, "end": e, "kwh": kwh, "heat": sum(1 for k in range(s, e, 60) if hstate.get(k, 0) >= .5)
               / ((e - s) / 60), "d_ah": None, "lpkwh": None, "lpkwh_sys": None,
               "dp_in": S.mean(dp(*ret_th[k]) for k in range(ms, me, 60) if k in ret_th)
               if any(k in ret_th for k in range(ms, me, 60)) else None}
        blower_kwh = sum(BLOWER_FAN_W for k in range(s, e, 60) if blower.get(k, 0) >= 0.5) / 60 / 1000
        if dl and len(dl) >= 15 and zm:
            ex = []                                  # per minute: (corrected ΔAH, airflow m3/h or None)
            for k, v in dl.items():
                xk = xdiff.get(k)
                erv_term = zm["beta"][2] * erv_cfm.get(k, 0) * xk / 100 if xk is not None else 0.0
                q = q_fan
                t_term = zm["beta"][6] * (t_ret[k] - T_REF) if k in t_ret else 0.0
                ex.append((v - zm["beta"][0] - erv_term - t_term, q))
            row["d_ah"] = S.mean(c for c, _ in ex)
            if all(q for _, q in ex):
                grams = -sum(c * q / 60 for c, q in ex)              # g removed over the measured minutes
                share = len(ex) / ((e - s) / 60)             # measured minutes / run minutes
                row["lpkwh"] = grams / 1000 / (kwh * share)
                row["lpkwh_sys"] = grams / 1000 / ((kwh + blower_kwh) * share)
        run_rows.append(row)
    eff = [r["lpkwh"] for r in run_rows if r["lpkwh"] is not None]
    # Revised 2026-11-09 (Hugh): compare runs at MATCHED inlet dew point. The E070 derates steeply below its 60 % RH
    # rating, so runs at different inlet conditions shouldn't agree (11-01→04 at dp ≈ 7 °C scored 0.2–0.4, the block's
    # first run at ≈ 9 °C 0.57). Fit L/kWh = a + b·(dp_in − DP_REF) over the runs; agreement = the residual scatter
    # around that line, plausibility = each run in range and efficiency not falling as the inlet gets wetter.
    pts = [(r["dp_in"], r["lpkwh"]) for r in run_rows if r["lpkwh"] is not None and r["dp_in"] is not None]
    dline = None
    if len(pts) >= 3 and len({round(x, 1) for x, _ in pts}) >= 2:
        fl = ols([[1.0, x - DP_REF] for x, _ in pts], [y for _, y in pts])
        if fl is not None:
            (a0, b1), (sa, sb) = fl[0], fl[1]
            res = [y - (a0 + b1 * (x - DP_REF)) for x, y in pts]
            mid = a0 + b1 * (S.median(x for x, _ in pts) - DP_REF)
            dline = {"a": a0, "b": b1, "sb": sb, "rsd": math.sqrt(sum(v * v for v in res) / max(len(pts) - 2, 1)),
                     "mid": mid}
    rel = dline["rsd"] / dline["mid"] if dline and dline["mid"] > 0 else None
    agree = rel is not None and len(pts) >= 5 and (rel <= 0.20 or (len(pts) >= 10 and rel <= 0.30))
    bins = []
    for lo, hi in DP_BINS:
        ys = [y for x, y in pts if lo <= x < hi]
        if ys:
            bins.append(f"dp {'<' + str(hi) if lo < -50 else ('≥' + str(lo) if hi > 50 else f'{lo}–{hi}')} °C: "
                        f"{len(ys)} run(s), mean {S.mean(ys):.2f}")
    not_falling = dline is not None and dline["b"] >= -2 * (dline["sb"] or 0)
    g2b = [
        crit("≥ 5 Aprilaire runs measured across the air handler (calls over 4 h count per 4-h segment)", len(eff) >= 5,
             f"{len(eff)} with L/kWh, {sum(1 for r in run_rows if r['d_ah'] is not None)} with ΔAH, "
             f"from {len(apr_runs)} calls since the study start"),
        crit("Runs agree at matched inlet dew point: scatter about the L/kWh-vs-dew-point line ≤ 20 % of its value at "
             "the median dew point (≤ 30 % with ≥ 10 runs)", agree,
             (f"line {dline['a']:.2f} {dline['b']:+.2f}/°C at {DP_REF} °C, residual SD {dline['rsd']:.2f} = "
              f"{rel:.0%} of {dline['mid']:.2f}; " if dline and rel is not None else "need ≥ 3 runs at ≥ 2 dew points; ")
             + ("; ".join(bins) or "no runs with an inlet dew point")),
        crit("Efficiency plausible (each run 0.1–2.5 L/kWh; not falling as the inlet gets wetter; E070 rated 1.8–2.1 "
             "at 60 % RH)", bool(eff) and all(0.1 <= x <= 2.5 for x in eff) and not_falling,
             ", ".join(f"{y:.2f} @ {x:.1f} °C" for x, y in pts[-8:]) or "no runs yet"),
    ]
    gates.append(("G2b", "Phase 2 done (method B live)", g2b))

    # ---- P3: rotation coverage (scheduler writes control.db study_block) ----------------------------------
    p3 = []
    try:
        cc = sqlite3.connect(f"file:{CONTROL}?mode=ro", uri=True)
        blocks = cc.execute("SELECT arm, inlet_dp_c, l_per_kwh FROM study_block WHERE end_reason='complete'").fetchall()
    except sqlite3.Error:
        blocks = None
    if blocks is None:
        p3.append(crit("Rotation running", False, "not started: the scheduler (Phase 3) isn't built yet"))
    else:
        by = {a: [b for b in blocks if b[0] == a] for a in P3_ARMS}
        p3.append(crit("Each arm ≥ 8 valid blocks; baseline ≥ 10",
                       all(len(by[a]) >= 8 for a in P3_ARMS) and len(by["0"]) >= 10,
                       ", ".join(f"{a}:{len(by[a])}" for a in P3_ARMS)))
        for unit, arms in (("Midea", ("M", "MA")), ("Aprilaire", ("A", "MA"))):
            xs = [b for a in arms for b in by[a]]
            bins = [sum(1 for b in xs if b[1] is not None and lo <= b[1] < hi) for lo, hi in DP_BINS]
            p3.append(crit(f"{unit}: ≥ 2 dew-point bins with ≥ 4 blocks", sum(1 for n in bins if n >= 4) >= 2,
                           " / ".join(f"{lo if lo > -99 else ''}–{hi if hi < 99 else ''} °C: {n}"
                                      for (lo, hi), n in zip(DP_BINS, bins))))
            v = [b[2] for b in xs if b[2] is not None]
            ci = 1.96 * S.stdev(v) / math.sqrt(len(v)) / S.mean(v) if len(v) >= 3 and S.mean(v) > 0 else None
            p3.append(crit(f"{unit}: L/kWh 95 % CI within ±15 %", ci is not None and ci <= 0.15,
                           f"{S.mean(v):.2f} L/kWh ± {ci:.0%}" if ci is not None else "need ≥ 3 measured blocks"))
    gates.append(("P3", "Rotation coverage → Phase 4", p3))

    # ---- status ----------------------------------------------------------------------------------------------
    status = {"generated": pt(now).isoformat(), "gates": []}
    for gid, title, cr in gates:
        due = DUE[gid]
        if gid == "P3":
            while due < today:
                due += timedelta(days=7)
        done = all(x["ok"] for x in cr)
        status["gates"].append({"id": gid, "title": title, "due": DUE[gid].isoformat(), "next_check": due.isoformat(),
                                "complete": done, "met": sum(1 for x in cr if x["ok"]), "of": len(cr),
                                "behind": (not done) and today > DUE[gid],
                                "check_today": today in (DUE[gid], due) and not done,
                                "criteria": cr})
    open_g = [g for g in status["gates"] if not g["complete"]]
    status["current"] = open_g[0]["id"] if open_g else "done"
    print(f"<!-- status: {json.dumps(status, ensure_ascii=False)} -->")

    # ---- report ----------------------------------------------------------------------------------------------
    print(f"# Dehumidifier study — readiness {pt(now):%Y-%m-%d %H:%M %Z}\n")
    for g in status["gates"]:
        mark = "✅ complete" if g["complete"] else ("⚠️ behind" if g["behind"] else "⏳ open")
        print(f"## {g['id']} — {g['title']} ({mark}, {g['met']}/{g['of']}; due {g['due']}"
              f"{'' if g['complete'] else ', next check ' + g['next_check']})\n")
        for x in g["criteria"]:
            print(f"- {'✅' if x['ok'] else '❌'} {x['name']}: {x['detail']}")
        print()

    # daily statistics, last 24 h
    print("## Last 24 h\n")
    o, lr = d.th(OUTDOOR, day_a, now), d.th(LR, day_a, now)
    rooms = [d.ah_series(r, day_a, now) for r in ROOMS]
    hav = {k: S.mean(r[k] for r in rooms) for k in rooms[0] if all(k in r for r in rooms)}
    odp = [dp(*v) for v in o.values()]
    idp = [dp(*v) for v in lr.values()]
    common = [k for k in o if k in hav]
    drier = sum(1 for k in common if ah(*o[k]) < hav[k]) / len(common) if common else float("nan")
    tmin = min((min(d.series(r, "temperature_c", day_a, now).values() or [99]) for r in ROOMS), default=99)
    a24 = [v for k, v in apr.items() if k >= day_a]
    r24 = [r for r in run_rows if r["end"] > day_a]
    hs = [v for k, v in hstate.items() if k >= day_a]
    bl = [v for k, v in blower.items() if k >= day_a]
    erv = list(d.series("erv_attic", "supply_cfm", day_a, now).values())
    mk = sum(v for k, v in mid_p.items() if k >= day_a) / 60 / 1000
    f = lambda xs, n=1: f"{S.mean(xs):.{n}f} ({min(xs):.{n}f}–{max(xs):.{n}f})" if xs else "—"
    print("| | mean (min–max) |\n|---|---|")
    print(f"| Outdoor dew point °C | {f(odp)} |\n| Living-room dew point °C | {f(idp)} |")
    print(f"| Living-room RH % | {f([v[1] for v in lr.values()], 0)} |")
    print(f"| House-average AH g/m³ | {f(list(hav.values()), 2)} |")
    print(f"| Hours outdoor air drier than house | {drier:.0%} |")
    print(f"| Coldest room min °C | {tmin:.1f} |")
    print(f"| Aprilaire | {sum(1 for v in a24 if v > APR_ON_W) / 60:.1f} h compressor, "
          f"{sum(a24) / 60 / 1000:.2f} kWh, {len(r24)} run(s) |")
    print(f"| Midea (plug) | {mk:.2f} kWh |")
    print(f"| Heat call / blower on | {S.mean(hs) if hs else 0:.0%} / {S.mean(bl) if bl else 0:.0%} |")
    print(f"| ERV supply cfm | {f(erv, 0)} |\n")
    if r24:
        print("| Aprilaire run (PT) | min | kWh | heat share | inlet dp °C | ΔAH corr g/m³ | L/kWh | L/kWh incl. blower |\n"
              "|---|---|---|---|---|---|---|---|")
        for r in r24:
            print(f"| {pt(r['start']):%m-%d %H:%M}–{pt(r['end']):%H:%M} | {(r['end'] - r['start']) // 60} | "
                  f"{r['kwh']:.2f} | {r['heat']:.0%} | {'—' if r['dp_in'] is None else format(r['dp_in'], '.1f')} | "
                  f"{'—' if r['d_ah'] is None else format(r['d_ah'], '+.3f')} | "
                  f"{'—' if r['lpkwh'] is None else format(r['lpkwh'], '.2f')} | "
                  f"{'—' if r['lpkwh_sys'] is None else format(r['lpkwh_sys'], '.2f')} |")
        print()
    missing = [k for k, v in MANUAL.items() if v is None]
    if missing:
        print(f"Waiting on Hugh: {', '.join(missing)} (edit MANUAL in tools/dehum_readiness.py).")
    return {"runs": run_rows, "zero_ok": g2a[1]["ok"]}


# tools/dehum_drift.py bundles this file as a library (it sets _AS_LIBRARY before this source); don't run then
if __name__ == "__main__" and not globals().get("_AS_LIBRARY"):
    main()
