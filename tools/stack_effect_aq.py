#!/usr/bin/env python3
"""Stack effect / furnace runs vs indoor air quality (Hugh, 2026-10-25). Read-only; run on the dictator.

Hypothesis under test: with a warm house over an unsealed crawlspace (abandoned duct runs still open to floor
registers), the house is depressurised at the bottom — more as the indoor-outdoor temperature difference grows,
and more again while the furnace burns (an 80%-type furnace sends house air up the flue). Crawlspace air then
comes up through the floor. Expected signatures:
  - by hour of day: a morning bump on the H side, a stronger afternoon bump on the C side;
  - by day: bigger bumps on days with a bigger temperature difference;
  - per furnace run: house moisture (absolute humidity) moves toward the crawlspace/outdoor level, VOCs rise.

Inputs: the hourly rung (rungs.db, ADR-0022) for everything; raw readings (parquet + hot.db) only to find furnace
runs, which the system never recorded (the thermostat was cloud-only) — reconstructed from fast temperature rises
at the mech-closet gas node, which sits next to the furnace (validated on the 2026-10-25 18:35 PDT run).

"Worse air" per side = mean of per-sensor z-scores, oriented so + = worse: SGP4x voc_raw and BME680 gas_ohm fall as
VOCs rise, SGP30 tvoc rises. Raw signals, not the self-adapting VOC index.

    venv/bin/python tools/stack_effect_aq.py [--since 2026-10-05] [--rise-c 1.5]
"""
import argparse
import datetime as dt
import math
import sqlite3
from collections import defaultdict
from statistics import mean, pstdev
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
GAS = {  # side -> [(device, metric, sign: +1 if a higher value is worse air)]
    "H": [("gas_h_office_sgp41", "voc_raw", -1), ("gas_h_office", "gas_ohm", -1), ("gas_hbed", "gas_ohm", -1)],
    "C": [("gas_c_office", "voc_raw", -1), ("gas_c_bed", "tvoc", +1)],
    "K": [("gas_kitchen", "voc_raw", -1)],
}
METERS = {"H": ["meter_pro_h_bed", "meter_h_bed_closet", "meter_h_bathroom", "meter_master_bath"],
          "C": ["meter_c_bed", "meter_pro_c_office"],
          "K": ["meter_kitchen", "meter_pro_living_room", "meter_living_room"]}
OUT, CRAWL, ATTIC, RADON, CLOSET = "switchbot_outdoor", "ometer_crawlspace", "meter_attic", "radon_crawlspace", "gas_mech_closet"


def ah(t, rh):
    """Absolute humidity g/m³ from °C and %RH (Magnus)."""
    return 6.112 * math.exp(17.67 * t / (t + 243.5)) * rh * 2.1674 / (273.15 + t)


def hourly(rung_db, since_ep):
    """{(device, metric): {bucket_start: mean}} from the 1-hour rung."""
    c = sqlite3.connect(f"file:{rung_db}?mode=ro", uri=True)
    s = defaultdict(dict)
    for d, m, b, v in c.execute("SELECT device_id, metric, bucket_start, vmean FROM rung WHERE res='1hour' "
                                "AND bucket_start >= ?", (since_ep,)):
        s[(d, m)][b] = v
    return s


def zscored(series):
    vals = list(series.values())
    if len(vals) < 24 or pstdev(vals) == 0:
        return {}
    mu, sd = mean(vals), pstdev(vals)
    return {b: (v - mu) / sd for b, v in series.items()}


def side_scores(s):
    out = {}
    for side, members in GAS.items():
        zs = [{b: sign * z for b, z in zscored(s.get((d, m), {})).items()} for d, m, sign in members]
        buckets = set().union(*zs) if zs else set()
        out[side] = {b: mean(z[b] for z in zs if b in z) for b in buckets}
    return out


def ah_series(s, devs):
    out = defaultdict(list)
    for d in devs:
        t, rh = s.get((d, "temperature_c"), {}), s.get((d, "humidity_pct"), {})
        for b in t.keys() & rh.keys():
            out[b].append(ah(t[b], rh[b]))
    return {b: mean(v) for b, v in out.items()}


def temp_series(s, devs):
    out = defaultdict(list)
    for d in devs:
        for b, v in s.get((d, "temperature_c"), {}).items():
            out[b].append(v)
    return {b: mean(v) for b, v in out.items()}


def pt(b):
    return dt.datetime.fromtimestamp(b, PT)


def pearson(xs, ys):
    if len(xs) < 4:
        return None
    mx, my = mean(xs), mean(ys)
    sx, sy = pstdev(xs), pstdev(ys)
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (len(xs) * sx * sy)


def furnace_hours(s, devs, min_rise_c, min_share):
    """Hours in which the WHOLE HOUSE warmed at once — the furnace's signature (a mech-closet temperature rise alone
    is not: something else in that closet cycles every 30–60 min). -> [(bucket_start, mean_rise_c, share_rising)].
    Daytime sun also warms rooms, but unevenly and slowly; report and let the reader judge."""
    temps = [s.get((d, "temperature_c"), {}) for d in devs]
    out = []
    for b in sorted(set().union(*temps)):
        rises = [t[b] - t[b - 3600] for t in temps if b in t and (b - 3600) in t]
        if len(rises) < 4:
            continue
        share = sum(r >= min_rise_c for r in rises) / len(rises)
        if share >= min_share:
            out.append((b, mean(rises), share))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", default="2026-10-05")
    ap.add_argument("--rung-db", default="instance/db/rungs.db")
    ap.add_argument("--hot-db", default="instance/db/hot.db")
    ap.add_argument("--parquet", default="instance/db/parquet/year=*/month=*/*.parquet")
    ap.add_argument("--rise-c", type=float, default=0.5, help="hour-over-hour °C rise a room must show")
    ap.add_argument("--share", type=float, default=0.75, help="fraction of main-floor rooms that must rise together")
    a = ap.parse_args()
    since = dt.datetime.fromisoformat(a.since).replace(tzinfo=PT)
    s = hourly(a.rung_db, int(since.timestamp()))

    score = side_scores(s)
    side_ah = {k: ah_series(s, v) for k, v in METERS.items()}
    t_main = temp_series(s, sum(METERS.values(), []))
    t_out, t_attic, t_crawl = (s.get((d, "temperature_c"), {}) for d in (OUT, ATTIC, CRAWL))
    radon = s.get((RADON, "radon_bqm3"), {})
    ah_out, ah_crawl = ah_series(s, [OUT]), ah_series(s, [CRAWL])
    d_stack = {b: t_main[b] - t_out[b] for b in t_main.keys() & t_out.keys()}
    d_ac = {b: t_attic[b] - t_crawl[b] for b in t_attic.keys() & t_crawl.keys()}
    days = sorted({pt(b).date() for b in d_stack})
    print(f"window {a.since} .. now (PT), {len(days)} days with indoor+outdoor temperature\n")

    # ── 1. hour-of-day profile ────────────────────────────────────────────────────────────────────────────
    print("1. HOUR-OF-DAY PROFILE (PT, averaged over the window). worse-air scores: + = worse (z units)")
    print("hour  in-out°C  attic-crawl°C  radon  H-worse  C-worse  K-worse   AH_H   AH_C   AH_K  AH_out AH_crawl")

    def by_hour(series):
        g = defaultdict(list)
        for b, v in series.items():
            g[pt(b).hour].append(v)
        return {h: mean(v) for h, v in g.items()}
    cols = [by_hour(x) for x in (d_stack, d_ac, radon, score["H"], score["C"], score["K"], side_ah["H"],
                                 side_ah["C"], side_ah["K"], ah_out, ah_crawl)]
    fmt = ["{:8.1f}", "{:13.1f}", "{:6.0f}", "{:8.2f}", "{:8.2f}", "{:8.2f}", "{:6.2f}", "{:6.2f}", "{:6.2f}",
           "{:7.2f}", "{:8.2f}"]
    for h in range(24):
        print(f"{h:02d}   " + " ".join((f.format(c[h]) if h in c else " " * len(f.format(0))) for f, c in zip(fmt, cols)))

    # ── 2. day-to-day: does a bigger temperature difference give a bigger bump? ───────────────────────────
    print("\n2. DAY BY DAY — bump = window mean minus that day's 01–04 h baseline (z units)")
    print("   morning 06–10 h -> H side;  afternoon 14–19 h -> C side")

    def window(series, day, h0, h1):
        v = [x for b, x in series.items() if pt(b).date() == day and h0 <= pt(b).hour < h1]
        return mean(v) if v else None
    print("day         in-out(am) H-bump   in-out(pm) attic-crawl(pm) C-bump")
    rows = []
    for day in days:
        base_h, base_c = window(score["H"], day, 1, 5), window(score["C"], day, 1, 5)
        am, pm = window(d_stack, day, 6, 11), window(d_stack, day, 14, 20)
        acpm = window(d_ac, day, 14, 20)
        hb = None if base_h is None or window(score["H"], day, 6, 11) is None else window(score["H"], day, 6, 11) - base_h
        cb = None if base_c is None or window(score["C"], day, 14, 20) is None else window(score["C"], day, 14, 20) - base_c
        rows.append((day, am, hb, pm, acpm, cb))
        f = lambda x, w: (f"{x:{w}.2f}" if x is not None else " " * w)
        print(f"{day}  {f(am, 9)} {f(hb, 7)}   {f(pm, 9)} {f(acpm, 15)} {f(cb, 7)}")
    for label, xi, yi in (("H morning bump vs in-out ΔT", 1, 2), ("C afternoon bump vs in-out ΔT", 3, 5),
                          ("C afternoon bump vs attic-crawl ΔT", 4, 5)):
        pairs = [(r[xi], r[yi]) for r in rows if r[xi] is not None and r[yi] is not None]
        r = pearson([p[0] for p in pairs], [p[1] for p in pairs])
        print(f"   r({label}) = {'n/a' if r is None else f'{r:+.2f}'}  (n={len(pairs)} days)")

    # ── 3. likely furnace runs: the whole house warmed together ──────────────────────────────────────────
    print(f"\n3. LIKELY FURNACE HOURS — >= {a.share:.0%} of main-floor rooms up >= {a.rise_c} °C hour-over-hour."
          "\n   Change from the hour before to the hour after (AH g/m³; worse-air z).")
    runs = furnace_hours(s, sum(METERS.values(), []), a.rise_c, a.share)
    print("hour (PT)          rise°C rooms  ΔAH_H  ΔAH_C  ΔAH_K  ΔH-worse ΔC-worse ΔK-worse  AH_out AH_crawl")
    f = lambda x, w=6: (f"{x:+{w}.2f}" if x is not None else " " * w)
    for b, rise, share in runs:
        before, after = b - 3600, b + 3600

        def d(series):
            return (series[after] - series[before]) if before in series and after in series else None
        print(f"{pt(b):%Y-%m-%d %H:00}   {rise:5.1f}  {share:4.0%} {f(d(side_ah['H']))} {f(d(side_ah['C']))} "
              f"{f(d(side_ah['K']))}   {f(d(score['H']))}   {f(d(score['C']))}   {f(d(score['K']))}  "
              f"{ah_out.get(before, float('nan')):6.2f} {ah_crawl.get(before, float('nan')):8.2f}")
    if not runs:
        print("   none found")
    print("\n   From 2026-10-25 22:06 PDT on, the thermostat records real heat/cool/blower state (thermostat_hall"
          " hvac_state, hvac_blower running) — use those instead of this reconstruction.")


if __name__ == "__main__":
    main()
