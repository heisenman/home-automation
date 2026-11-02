#!/usr/bin/env python3
"""Mech-closet air quality vs the furnace: how much of a heat call do the closet's gas sensors notice?
(Hugh, 2026-11-02: "how much combustion gas is noticed by the sensors in the same room".)

Read-only on ha-2's rungs.db; stdlib only, piped like the dehumidifier-study tools:

    ssh 192.168.1.200 'cd ~/home_automation && python3 - [--since 2026-10-26]' < tools/furnace_closet_aq.py

Filed daily on .210 by tools/ha2_report.sh (cron) under instance/furnace-aq/reports/.

The catch is that a heat call also starts the blower, and the blower alone changes the closet's air (it cools by
~1 °C and the VOC signal moves, from air drawn through the closet). So events are split three ways and compared
at the same offsets from their start:

  A  heat call, blower was OFF   → combustion + air movement
  B  heat call, blower already ON (running with the Aprilaire) → combustion only; the cleanest test
  C  blower start, no heat (Aprilaire follow, fan-only blocks) → air movement only; the control

Combustion signal = B directly, and A − C. Sensors: BME680 `gas_mech_closet` (gas resistance — falls with
reducing gases such as CO and VOCs; temperature, humidity) and SGP41 `gas_mech_closet_sgp41` (raw VOC and NOx
signals, plus the indexes). The SGP41's indexes restart with the node, and that node rebooted every 40–80 s until
the splitter swap on 2026-10-31 ~16:20 PDT, so raw signals are primary and index rows before then are flagged.
"""
import argparse
import math
import sqlite3
import statistics as S
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
RUNGS = "instance/db/rungs.db"
BME, SGP = "gas_mech_closet", "gas_mech_closet_sgp41"
METRICS = [  # (device, metric, mode, label); mode "pct" = % change of the value, "d" = difference
    (BME, "gas_ohm", "pct", "BME680 gas resistance %"),
    (SGP, "voc_raw", "d", "SGP41 VOC raw (falls as VOC rises)"),
    (SGP, "nox_raw", "d", "SGP41 NOx raw"),
    (SGP, "voc_index", "d", "SGP41 VOC index"),
    (SGP, "nox_index", "d", "SGP41 NOx index"),
    (BME, "temperature_c", "d", "closet temperature °C"),
    (BME, "humidity_pct", "d", "closet RH %"),
]
OFFS = [3, 6, 10, 15, 20, 30]
EVENTS = [("2026-10-26 12:00", "2026-10-28 06:00"), ("2026-10-29 16:00", "2026-10-30 06:00")]   # foam, waxing
SPLITTER_FIX = datetime(2026, 10, 31, 16, 20, tzinfo=PT).timestamp()


def pt_ep(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=PT).timestamp())


def series(c, dev, metric, a, b):
    return dict(c.execute("SELECT bucket_start, vmean FROM rung WHERE res='1min' AND device_id=? AND metric=? "
                          "AND bucket_start>=? AND bucket_start<?", (dev, metric, a, b)))


def response(D, e, mode, base_min=15):
    """Change at each offset (3-min mean centred) against the mean of the base_min minutes before the start."""
    base = [D[e - 60 * i] for i in range(2, base_min + 2) if e - 60 * i in D]
    if len(base) < base_min // 2:
        return None
    b = S.mean(base)
    out = []
    for o in OFFS:
        v = [D[e + 60 * (o + j)] for j in (-1, 0, 1) if e + 60 * (o + j) in D]
        if not v:
            out.append(None)
        elif mode == "pct":
            out.append(100 * (S.mean(v) / b - 1) if b > 0 else None)
        else:
            out.append(S.mean(v) - b)
    return out


def summarise(rows):
    cols = []
    for i in range(len(OFFS)):
        col = [r[i] for r in rows if r[i] is not None]
        cols.append((S.mean(col), S.stdev(col) / math.sqrt(len(col)), len(col)) if len(col) >= 2 else None)
    return cols


def fmt(cols):
    return " | ".join("—" if x is None else f"{x[0]:+.2f} ± {x[1]:.2f}" for x in cols)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-10-26", help="PT date (Venstar furnace state starts 2026-10-26)")
    args = ap.parse_args()
    a = pt_ep(args.since + " 00:00")
    b = int(datetime.now(timezone.utc).timestamp()) // 60 * 60
    c = sqlite3.connect(f"file:{RUNGS}?mode=ro", uri=True)
    hs = series(c, "thermostat_hall", "hvac_state", a, b)
    st = series(c, "thermostat_hall", "active_stage", a, b)
    bl = series(c, "hvac_blower", "running", a, b)
    ev_win = [(pt_ep(x), pt_ep(y)) for x, y in EVENTS]

    def excluded(k):
        return any(x <= k < y for x, y in ev_win)

    heat = lambda k: hs.get(k, 0) >= 0.5
    blow = lambda k: bl.get(k, 0) >= 0.5
    A, B, C = [], [], []
    for k in sorted(hs):
        if heat(k) and not heat(k - 60) and k - 60 in hs and not excluded(k):
            prior_blower = [blow(k - 60 * i) for i in range(2, 12)]
            if not any(prior_blower):
                A.append(k)
            elif all(prior_blower):
                B.append(k)
    for k in sorted(bl):
        if blow(k) and not blow(k - 60) and k - 60 in bl and not excluded(k) \
                and not any(heat(k + 60 * i) for i in range(-15, 31)):
            C.append(k)
    dur = {}
    for e in A + B:
        n = 0
        while heat(e + 60 * n) and n < 180:
            n += 1
        dur[e] = n

    print(f"# Mech closet air vs furnace — {datetime.now(PT):%Y-%m-%d %H:%M %Z}\n")
    print(f"Window {datetime.fromtimestamp(a, PT):%Y-%m-%d} → now. Heat calls: **A** {len(A)} with the blower off "
          f"before, **B** {len(B)} with the blower already running (combustion only); **C** {len(C)} blower starts "
          f"with no heat (air movement only). Median heat call {S.median(dur.values()) if dur else 0:.0f} min; "
          f"stage 2 in {sum(1 for e in A + B if st.get(e + 300, 1) >= 1.5)} of {len(A) + len(B)}. "
          "Values: mean change from the 15 min before the start, ± SE.\n")
    print(f"| Metric | Class | n | " + " | ".join(f"+{o} min" for o in OFFS) + " |")
    print("|---|---|---|" + "---|" * len(OFFS))
    verdict = []
    for dev, metric, mode, label in METRICS:
        D = series(c, dev, metric, a - 3600, b)
        res = {}
        for name, evs in (("A", A), ("B", B), ("C", C)):
            use = [e for e in evs if not (metric.endswith("_index") and dev == SGP and e < SPLITTER_FIX)]
            rows = [r for r in (response(D, e, mode) for e in use) if r is not None]
            res[name] = summarise(rows) if len(rows) >= 2 else None
            print(f"| {label} | {name} | {len(rows)} | " + (fmt(res[name]) if res[name] else " | ".join("—" for _ in OFFS))
                  + " |")
        # A − C at each offset (difference of means, SEs in quadrature) = what heat adds beyond air movement
        if res["A"] and res["C"]:
            diff = [None if x is None or y is None else (x[0] - y[0], math.hypot(x[1], y[1]), 0)
                    for x, y in zip(res["A"], res["C"])]
            print(f"| {label} | **A − C** | | " + fmt(diff) + " |")
            sig = [(o, d) for o, d in zip(OFFS, diff) if d and abs(d[0]) > 2 * d[1]]
            verdict.append((label, "A − C", sig))
        if res["B"]:
            sig = [(o, d) for o, d in zip(OFFS, res["B"]) if d and abs(d[0]) > 2 * d[1]]
            verdict.append((label, "B", sig))
    print("\n## Read-out\n")
    print("A change counts as noticed when it exceeds twice its SE.\n")
    for label, cls, sig in verdict:
        if sig:
            print(f"- **{label}** ({cls}): " + ", ".join(f"+{o} min {d[0]:+.2f} ± {d[1]:.2f}" for o, d in sig))
        else:
            print(f"- {label} ({cls}): nothing beyond noise")
    if not B:
        print("\nNo class-B events yet (heat call while the blower already runs). They come from heat calls during "
              "Aprilaire runs and are the cleanest combustion test.")


if __name__ == "__main__":
    main()
