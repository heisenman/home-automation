#!/usr/bin/env python3
"""Phase 0 data audit for the dehumidifier efficiency study (docs/design/dehumidifier-efficiency-study.md §6).

Read-only over ha-2's rollups (instance/db/rungs.db, hourly) and weather.db. Stdlib only, so it runs on ha-2:

    ssh 192.168.1.200 'cd ~/home_automation && python3 -' < tools/dehum_audit.py > report.md
    python3 tools/dehum_audit.py --since 2026-06-24          # on ha-2, from the checkout

Sections: A coverage of every study variable, B agreement between sensors in the same space (offsets in T, RH and
absolute humidity, and how stable they are), C the Open-Meteo grid weather vs the local outdoor meter, D whether
each unit's reported state matches its metered power, E the room set proposed for the house moisture average.
All times are UTC in the queries and Pacific in the report.
"""
import argparse
import math
import sqlite3
import statistics as S
import sys
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    PT = ZoneInfo("America/Los_Angeles")
except Exception:                       # noqa: BLE001 — report in UTC if tzdata is missing
    PT = timezone.utc

RUNGS = "instance/db/rungs.db"
WEATHER = "instance/db/weather.db"

# study variables: (device, metric, what it is)
STUDY = [
    ("meter_pro_living_room", "humidity_pct", "control sensor (both dehumidifiers)"),
    ("meter_living_room", "humidity_pct", "living room, 2nd meter"),
    ("meter_kitchen", "humidity_pct", "kitchen"),
    ("meter_c_bed", "humidity_pct", "C bed"),
    ("meter_pro_c_office", "humidity_pct", "C office"),
    ("meter_pro_h_bed", "humidity_pct", "H bed"),
    ("meter_h_bed_closet", "humidity_pct", "H bed closet"),
    ("meter_h_bathroom", "humidity_pct", "H bathroom (shower pulses)"),
    ("meter_master_bath", "humidity_pct", "master bath (shower pulses)"),
    ("meter_attic", "humidity_pct", "attic (Aprilaire + ERV live here)"),
    ("ometer_crawlspace", "humidity_pct", "crawlspace"),
    ("switchbot_outdoor", "humidity_pct", "outdoor, local"),
    ("dehumidifier_living_room", "mode", "Midea mode"),
    ("dehumidifier_living_room", "temperature_c", "Midea inlet T (onboard)"),
    ("plug_hall", "power_w", "Midea power"),
    ("dehum_attic", "unit_running", "Aprilaire running"),
    ("dehum_attic", "dh_call", "Aprilaire call"),
    ("dehum_attic", "unit_rh_pct", "Aprilaire inlet RH (onboard)"),
    ("dehum_pm", "power_w", "Aprilaire power"),
    ("erv_attic", "supply_cfm", "ERV supply"),
    ("hvac_blower", "running", "furnace blower"),
    ("thermostat_hall", "hvac_state", "furnace heat/cool"),
    ("study_midea_inlet", "humidity_pct", "study: Midea inlet"),
    ("study_midea_outlet", "humidity_pct", "study: Midea outlet"),
]

# sensors in the same space, or the comparisons the study leans on
PAIRS = [
    ("meter_pro_living_room", "meter_living_room", "living room: the two meters"),
    ("meter_pro_living_room", "dehumidifier_living_room", "living room: Midea onboard vs control sensor"),
    ("meter_pro_living_room", "study_midea_inlet", "living room: study inlet vs control sensor"),
    ("study_midea_inlet", "study_midea_outlet", "study pair (Midea idle = zero point)"),
    ("meter_pro_h_bed", "meter_h_bed_closet", "H bed vs its closet"),
    ("meter_pro_h_bed", "gas_hbed", "H bed: meter vs BME680"),
    ("meter_pro_c_office", "gas_h_office", "(C office meter vs H office BME680 — different rooms, reference only)"),
    ("ometer_crawlspace", "radon_crawlspace", "crawlspace: meter vs Aranet"),
]

ROOMS_FOR_AVERAGE = ["meter_pro_living_room", "meter_kitchen", "meter_c_bed", "meter_pro_c_office",
                     "meter_pro_h_bed"]


def ah(t, rh):
    """Absolute humidity g/m3 from T (C) and RH (%)."""
    return 6.112 * math.exp(17.67 * t / (t + 243.5)) * rh * 2.1674 / (273.15 + t)


def dewpoint(t, rh):
    g = math.log(max(rh, 0.1) / 100) + 17.62 * t / (243.12 + t)
    return 243.12 * g / (17.62 - g)


def hourly(c, dev, metric, a, b):
    return dict(c.execute("SELECT bucket_start, vmean FROM rung WHERE res='1hour' AND device_id=? AND metric=? "
                          "AND bucket_start>=? AND bucket_start<?", (dev, metric, a, b)))


def fmt_t(ep):
    return datetime.fromtimestamp(ep, PT).strftime("%Y-%m-%d %H:%M")


def section_coverage(c, a, b):
    out = ["## A. Coverage (hourly rollups)", "",
           f"Window {fmt_t(a)} → {fmt_t(b)} PT. % = hours with data since the variable's first reading in the "
           "window. Longest gap in hours.", "",
           "| Device · metric | What | First | % hours | Longest gap (h) | Gap starts |",
           "|---|---|---|---|---|---|"]
    for dev, m, what in STUDY:
        hs = sorted(hourly(c, dev, m, a, b))
        if not hs:
            out.append(f"| `{dev}` · {m} | {what} | — | 0 | — | — |")
            continue
        span = (b - hs[0]) // 3600
        gaps = [(hs[i + 1] - hs[i]) // 3600 - 1 for i in range(len(hs) - 1)] + [(b - hs[-1]) // 3600 - 1]
        gi = max(range(len(gaps)), key=gaps.__getitem__)
        out.append(f"| `{dev}` · {m} | {what} | {fmt_t(hs[0])[:10]} | {100 * len(hs) / max(span, 1):.1f} | "
                   f"{gaps[gi]} | {fmt_t(hs[gi]) if gaps[gi] > 0 else '—'} |")
    return out


def section_pairs(c, a, b):
    out = ["", "## B. Sensor agreement (hourly means where both report)", "",
           "Offset = second minus first. SD = standard deviation of the hourly difference (small SD + steady "
           "offset = a calibration constant; large SD = the two see different air).", "",
           "| Pair | Hours | ΔT °C (SD) | ΔRH % (SD) | ΔAH g/m³ (SD) |", "|---|---|---|---|---|"]
    for d1, d2, what in PAIRS:
        t1, h1 = hourly(c, d1, "temperature_c", a, b), hourly(c, d1, "humidity_pct", a, b)
        t2, h2 = hourly(c, d2, "temperature_c", a, b), hourly(c, d2, "humidity_pct", a, b)
        ks = [k for k in t1 if k in h1 and k in t2 and k in h2]
        if len(ks) < 12:
            out.append(f"| {what} | {len(ks)} | — | — | — |")
            continue
        dt = [t2[k] - t1[k] for k in ks]
        dh = [h2[k] - h1[k] for k in ks]
        da = [ah(t2[k], h2[k]) - ah(t1[k], h1[k]) for k in ks]
        out.append(f"| {what} | {len(ks)} | {S.mean(dt):+.2f} ({S.pstdev(dt):.2f}) | "
                   f"{S.mean(dh):+.1f} ({S.pstdev(dh):.1f}) | {S.mean(da):+.2f} ({S.pstdev(da):.2f}) |")
    return out


def section_outdoor(c, w, a, b):
    out = ["", "## C. Outdoor: Open-Meteo grid vs the local meter (`switchbot_outdoor`)", ""]
    om = {}
    for ts, metric, v in w.execute("SELECT ts, metric, value FROM weather WHERE source='openmeteo' AND "
                                   "metric IN ('temperature_c','humidity_pct') AND ts>=? AND ts<?",
                                   (datetime.fromtimestamp(a, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                    datetime.fromtimestamp(b, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))):
        ep = int(datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
        om.setdefault(ep, {})[metric] = v
    lt, lh = hourly(c, "switchbot_outdoor", "temperature_c", a, b), hourly(c, "switchbot_outdoor", "humidity_pct", a, b)
    ks = [k for k in lt if k in lh and k in om and len(om[k]) == 2]
    out.append(f"Open-Meteo hours in window: {len(om)}; overlapping with the local meter: {len(ks)} "
               f"(local meter since {fmt_t(min(lt))[:10] if lt else '—'}).")
    if len(ks) < 24:
        return out
    rows = []
    for k in ks:
        T, H = om[k]["temperature_c"], om[k]["humidity_pct"]
        rows.append((k, lt[k] - T, lh[k] - H, ah(lt[k], lh[k]) - ah(T, H),
                     dewpoint(lt[k], lh[k]) - dewpoint(T, H)))
    def stat(i):
        v = [r[i] for r in rows]
        return f"{S.mean(v):+.2f} (SD {S.pstdev(v):.2f}, RMS {math.sqrt(S.mean(x * x for x in v)):.2f})"
    out += ["", "Local minus grid:", "",
            f"- temperature °C: {stat(1)}", f"- RH %: {stat(2)}", f"- absolute humidity g/m³: {stat(3)}",
            f"- dew point °C: {stat(4)}", "", "By Pacific hour of day (local − grid), to spot sun on the local meter:", "",
            "| PT hour | n | ΔT °C | ΔAH g/m³ |", "|---|---|---|---|"]
    by = {}
    for r in rows:
        by.setdefault(datetime.fromtimestamp(r[0], PT).hour, []).append(r)
    for h in sorted(by):
        v = by[h]
        out.append(f"| {h:02d} | {len(v)} | {S.mean(x[1] for x in v):+.2f} | {S.mean(x[3] for x in v):+.2f} |")
    return out


def section_units(c, a, b):
    out = ["", "## D. Unit state vs metered power (hourly means)", ""]
    run, call, pw = (hourly(c, "dehum_attic", "unit_running", a, b), hourly(c, "dehum_attic", "dh_call", a, b),
                     hourly(c, "dehum_pm", "power_w", a, b))
    ks = sorted(k for k in run if k in pw)
    if ks:
        full = [pw[k] for k in ks if run[k] > 0.99]
        off = [pw[k] for k in ks if run[k] < 0.01]
        out += [f"**Aprilaire** ({len(ks)} hours with both): running-all-hour mean "
                f"{S.mean(full) if full else float('nan'):.0f} W (n {len(full)}); off-all-hour mean "
                f"{S.mean(off) if off else float('nan'):.1f} W (n {len(off)}).",
                f"Hours running with < 150 W mean (fan only / no compressor?): "
                f"{sum(1 for k in ks if run[k] > 0.99 and pw[k] < 150)}. "
                f"Hours called but not running: {sum(1 for k in ks if call.get(k, 0) > 0.99 and run[k] < 0.01)}.", ""]
        lo = [k for k in ks if run[k] > 0.99 and pw[k] < 150]
        if lo:
            out.append("Low-power running hours (first 10, PT): " + ", ".join(fmt_t(k) for k in lo[:10]))
            out.append("")
    mode, mp = hourly(c, "dehumidifier_living_room", "mode", a, b), hourly(c, "plug_hall", "power_w", a, b)
    ks = sorted(k for k in mode if k in mp)
    if ks:
        cont = [mp[k] for k in ks if mode[k] > 1.99]
        sett = [mp[k] for k in ks if mode[k] < 1.01]
        out.append(f"**Midea** ({len(ks)} metered hours): mode 2 all-hour mean "
                   f"{S.mean(cont) if cont else float('nan'):.0f} W (n {len(cont)}); mode 1 all-hour mean "
                   f"{S.mean(sett) if sett else float('nan'):.1f} W (n {len(sett)}).")
    return out


def section_average(c, a, b):
    out = ["", "## E. Proposed house moisture average", "",
           "Equal-weight mean of absolute humidity over: " + ", ".join(f"`{d}`" for d in ROOMS_FOR_AVERAGE) +
           ". Bathrooms (shower pulses), the H bed closet, the attic and the crawlspace are left out (they're "
           "sources, sinks or different spaces). Each room's mean deviation from that average, over the window:", "",
           "| Room sensor | Hours | mean ΔAH g/m³ | SD |", "|---|---|---|---|"]
    series = {}
    for d in ROOMS_FOR_AVERAGE + ["meter_living_room", "meter_h_bed_closet", "meter_h_bathroom",
                                  "meter_master_bath", "meter_attic", "ometer_crawlspace"]:
        t, h = hourly(c, d, "temperature_c", a, b), hourly(c, d, "humidity_pct", a, b)
        series[d] = {k: ah(t[k], h[k]) for k in t if k in h}
    hours = [k for k in series[ROOMS_FOR_AVERAGE[0]] if all(k in series[d] for d in ROOMS_FOR_AVERAGE)]
    avg = {k: S.mean(series[d][k] for d in ROOMS_FOR_AVERAGE) for k in hours}
    for d, s in series.items():
        ks = [k for k in hours if k in s]
        if len(ks) < 12:
            out.append(f"| `{d}` | {len(ks)} | — | — |")
            continue
        v = [s[k] - avg[k] for k in ks]
        out.append(f"| `{d}`{' (in average)' if d in ROOMS_FOR_AVERAGE else ''} | {len(ks)} | {S.mean(v):+.2f} | "
                   f"{S.pstdev(v):.2f} |")
    out += ["", f"Hours where all {len(ROOMS_FOR_AVERAGE)} averaged rooms report: {len(hours)}."]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-06-24", help="window start, a Pacific date (default 2026-06-24)")
    ap.add_argument("--pairs-days", type=int, default=30, help="days back for section B/E (default 30)")
    args = ap.parse_args()
    a = int(datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=PT).timestamp())
    b = int(datetime.now(timezone.utc).timestamp()) // 3600 * 3600
    pa = b - args.pairs_days * 86400
    c = sqlite3.connect(f"file:{RUNGS}?mode=ro", uri=True)
    w = sqlite3.connect(f"file:{WEATHER}?mode=ro", uri=True)
    lines = [f"# Dehumidifier study — Phase 0 data audit ({datetime.now(PT):%Y-%m-%d %H:%M %Z})", "",
             f"Generated by `tools/dehum_audit.py --since {args.since}` on ha-2. Sections B and E use the last "
             f"{args.pairs_days} days.", ""]
    lines += section_coverage(c, a, b)
    lines += section_pairs(c, pa, b)
    lines += section_outdoor(c, w, a, b)
    lines += section_units(c, a, b)
    lines += section_average(c, pa, b)
    print("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
