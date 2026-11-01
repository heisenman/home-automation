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
RUNGS, CONTROL = "instance/db/rungs.db", "instance/db/control.db"
RHO = 1.2                                       # kg/m3, air near 21 °C
CFM = 1.699                                     # m3/h per cfm

# Facts only Hugh can supply. Edit + commit when he answers (the report lists what is still missing).
MANUAL = {
    "fan_tap": None,            # continuous-fan (G) tap 1-5, read off the 59TP5 control board
    "furnace_venting": None,    # "two-pipe" (outdoor combustion air) | "one-pipe" (combustion air from the space)
    "static_in_wc": 0.3,        # assumed external static for the tap table (§2); refine if measured
}
# 59TP5A060 air delivery at 0.1 / 0.3 / 0.5 in. w.c. (59TP5A-02PD, §2)
TAP_CFM = {1: (785, 655, 530), 2: (1160, 1080, 1000), 3: (1010, 925, 835), 4: (1325, 1255, 1185),
           5: (1720, 1620, 1505)}

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
    # heat cycles are short (10-20 min), so the blower only has to hold 3 min; the Aprilaire must have been idle
    # 15 min so its last wet/dry air has cleared the ducts
    zero_min = [k for k in blower_on if held(blower_on, k, 3) and held(apr_idle, k, 15)]
    hv_zero = pair_delta(d, HV_RET, HV_SUP, zero_min, study_a, now) if reg else None
    hz = offset_stats(hv_zero) if hv_zero else None
    heat_share = (sum(1 for k in zero_min if hstate.get(k, 0) >= 0.5) / len(zero_min)) if zero_min else 0
    mid_idle = {k: mid_p.get(k, 999) < 5 for k in mid_p}
    mz = offset_stats(pair_delta(d, MID_IN, MID_OUT, [k for k in mid_idle if held(mid_idle, k, 60)], study_a, now)
                      or {})
    g2a = [
        crit("HVAC pair registered and reporting", fresh,
             "both reporting" if fresh else ("registered, not reporting in 6 h" if reg else
                                             f"not registered yet ({HV_RET}, {HV_SUP})")),
        crit("HVAC pair zero offset: ≥ 6 blower-only h over ≥ 2 days, SE ≤ 0.05 g/m³",
             bool(hz and hz["hours"] >= 6 and hz["days"] >= 2 and hz["se"] <= 0.05),
             (f"{hz['hours']:.1f} h in {hz['chunks']} stretches / {hz['days']} d, offset {hz['mean']:+.3f} ± {hz['se']:.3f} g/m³ "
              f"(stretch SD {hz['sd']:.3f}; {heat_share:.0%} of the minutes during heat)") if hz else
             f"{len(zero_min)} blower-only minutes so far, no paired meter data"),
        crit("Continuous-fan tap known", tap is not None, f"tap {tap} ≈ {q_fan / CFM:.0f} cfm" if tap else
             "read the tap the G (fan) terminal uses off the 59TP5 control board"),
        crit("Furnace venting known", MANUAL["furnace_venting"] is not None,
             MANUAL["furnace_venting"] or "two-pipe or one-pipe? (two PVC pipes outside, or intake open in closet)"),
        crit("Midea pair zero offset (≥ 12 idle h)", bool(mz and mz["hours"] >= 12),
             f"{mz['hours']:.1f} h, offset {mz['mean']:+.3f} ± {mz['se']:.3f} g/m³ (hourly SD {mz['sd']:.3f})"
             if mz else "not enough idle hours"),
    ]
    gates.append(("G2a", "HVAC meters zeroed", g2a))

    # ---- G2b: Aprilaire measured across the air handler ---------------------------------------------------
    run_rows = []
    for s, e in apr_runs:
        ms, me = s + 20 * 60, e                      # skip coil wetting / mixing
        if me - ms < 20 * 60:
            continue
        kwh = sum(apr.get(k, 0) for k in range(s, e, 60)) / 60 / 1000
        mins = [k for k in range(ms, me, 60) if blower.get(k, 0) >= 0.99]
        dl = pair_delta(d, HV_RET, HV_SUP, mins, ms, me) if reg else None
        row = {"start": s, "end": e, "kwh": kwh, "heat": sum(1 for k in range(s, e, 60) if hstate.get(k, 0) >= .5)
               / ((e - s) / 60), "d_ah": None, "lpkwh": None}
        if dl and len(dl) >= 15:
            corr = S.mean(dl.values()) - (hz["mean"] if hz else 0)
            row["d_ah"] = corr
            if q_fan and hz:
                frac = len(dl) / ((me - ms) / 60)
                g = -corr * q_fan * (me - ms) / 3600 * frac          # g removed over the measured part
                row["lpkwh"] = g / 1000 / (kwh * (me - ms) / (e - s))
        run_rows.append(row)
    eff = [r["lpkwh"] for r in run_rows if r["lpkwh"] is not None]
    spread = ((max(eff) - min(eff)) / 2 / S.median(eff)) if len(eff) >= 2 and S.median(eff) > 0 else None
    g2b = [
        crit("≥ 5 Aprilaire runs measured across the air handler", len(eff) >= 5,
             f"{len(eff)} with L/kWh, {sum(1 for r in run_rows if r['d_ah'] is not None)} with ΔAH, "
             f"{len(apr_runs)} runs since the study start"),
        crit("Run-to-run agreement within ±20 %", spread is not None and len(eff) >= 5 and spread <= 0.20,
             f"half-range {spread:.0%} of median {S.median(eff):.2f} L/kWh" if spread is not None else "need ≥ 2 runs"),
        crit("Efficiency plausible (0.3–2.5 L/kWh; E070 rated 1.8–2.1 at 60 % RH)",
             bool(eff) and all(0.3 <= x <= 2.5 for x in eff),
             ", ".join(f"{x:.2f}" for x in eff[-8:]) or "no runs yet"),
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
        print("| Aprilaire run (PT) | min | kWh | heat share | ΔAH corr g/m³ | L/kWh |\n|---|---|---|---|---|---|")
        for r in r24:
            print(f"| {pt(r['start']):%m-%d %H:%M}–{pt(r['end']):%H:%M} | {(r['end'] - r['start']) // 60} | "
                  f"{r['kwh']:.2f} | {r['heat']:.0%} | "
                  f"{'—' if r['d_ah'] is None else format(r['d_ah'], '+.3f')} | "
                  f"{'—' if r['lpkwh'] is None else format(r['lpkwh'], '.2f')} |")
        print()
    missing = [k for k, v in MANUAL.items() if v is None]
    if missing:
        print(f"Waiting on Hugh: {', '.join(missing)} (edit MANUAL in tools/dehum_readiness.py).")


if __name__ == "__main__":
    main()
