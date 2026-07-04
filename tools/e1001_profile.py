#!/usr/bin/env python3
"""E1001 battery-profiler analysis (Module 3, back-end).

Reads a capture from tools/e1001_capture.sh (lines: "<epoch_seconds> <json>") of the battery_profiler
telemetry (e1001-bench/battprofile: st,v,hiz,chg,pg,mah,cyc,ocv_d,ocv_c,floor) and derives a V->SoC profile.

The reference is the CHARGE leg: the SY6974B has no current sensor, but charge current is the known,
ILIM-capped I_set (~500mA), so the firmware coulomb-counts fast-charge -> mah at each sample. Total mah
(floor->full) is the usable capacity between ocv_d and ocv_c. Then:
  - CHARGE curve  : SoC = mah / total_mah, at the (charge-elevated) terminal V.
  - DISCHARGE curve: assuming ~constant load, SoC = 1 - (t - t0)/(t1 - t0), scaled to total_mah, at the
    (load-sagged) terminal V.
The rested OCV anchors are ocv_d (~floor SoC) and ocv_c (100%). True OCV lies between charge/discharge
(hysteresis); a v1 ha_battery_profile LUT can average them or use the discharge leg.

  usage: tools/e1001_profile.py fit <capture.jsonl> [--csv out.csv]
         tools/e1001_profile.py summary <capture.jsonl>
"""
import argparse
import json
import sys

ST = {0: "IDLE", 1: "DISCHARGE", 2: "REST_D", 3: "CHARGE", 4: "REST_C"}


def load(path):
    rows = []
    for ln in open(path):
        ln = ln.strip()
        if not ln or ln[0] == "#" or " " not in ln:
            continue
        ts, _, js = ln.partition(" ")
        if js.lstrip().startswith("#"):
            continue
        try:
            d = json.loads(js)
            d["t"] = float(ts)
            rows.append(d)
        except (ValueError, TypeError):
            continue
    return rows


def segments(rows):
    """Split into contiguous same-state runs: list of (state, [rows])."""
    segs, cur, st = [], [], None
    for r in rows:
        if r.get("st") != st:
            if cur:
                segs.append((st, cur))
            cur, st = [], r.get("st")
        cur.append(r)
    if cur:
        segs.append((st, cur))
    return segs


def summary(path):
    rows = load(path)
    if not rows:
        print("no rows", file=sys.stderr)
        return
    span = (rows[-1]["t"] - rows[0]["t"]) / 3600.0
    print(f"{len(rows)} samples over {span:.2f} h  V {rows[0].get('v')} -> {rows[-1].get('v')}")
    for st, seg in segments(rows):
        dur = (seg[-1]["t"] - seg[0]["t"]) / 60.0
        v0, v1 = seg[0].get("v"), seg[-1].get("v")
        mah = seg[-1].get("mah", 0)
        print(f"  {ST.get(st, st):9} {dur:6.1f} min  V {v0:.3f}->{v1:.3f}  mah={mah:.1f} "
              f"ocv_d={seg[-1].get('ocv_d',0):.3f} ocv_c={seg[-1].get('ocv_c',0):.3f}")


def fit(path, csv_out=None):
    rows = load(path)
    segs = segments(rows)
    disch = [s for st, s in segs if st == 1]
    charge = [s for st, s in segs if st == 3]
    if not charge:
        print("no completed CHARGE leg yet — need a full discharge->charge cycle for the SoC reference",
              file=sys.stderr)
        if disch:
            d = disch[-1]
            print(f"(discharge leg present: {len(d)} samples, V {d[0]['v']:.3f}->{d[-1]['v']:.3f} — "
                  f"V-vs-time captured, but no capacity anchor until it charges back)", file=sys.stderr)
        return
    ch = charge[-1]
    total = ch[-1].get("mah", 0) or max((r.get("mah", 0) for r in ch), default=0)
    if total <= 0:
        print("charge leg has no coulombs — cannot scale SoC", file=sys.stderr)
        return
    ocv_c = ch[-1].get("ocv_c") or next((r.get("ocv_c") for r in reversed(rows) if r.get("ocv_c")), 0)
    print(f"# usable capacity floor->full: {total:.1f} mAh   OCV_full(anchor)={ocv_c:.3f}")
    out = [("leg", "soc_pct", "v")]
    for r in ch:
        soc = 100.0 * r.get("mah", 0) / total
        out.append(("charge", f"{soc:.1f}", f"{r.get('v'):.3f}"))
    if disch:
        d = disch[-1]
        t0, t1 = d[0]["t"], d[-1]["t"]
        span = max(t1 - t0, 1e-6)
        for r in d:
            soc = 100.0 * (1.0 - (r["t"] - t0) / span)  # constant-current assumption
            out.append(("discharge", f"{soc:.1f}", f"{r.get('v'):.3f}"))
    if csv_out:
        with open(csv_out, "w") as f:
            for row in out:
                f.write(",".join(map(str, row)) + "\n")
        print(f"# wrote {len(out)-1} points -> {csv_out}")
    else:
        for row in out:
            print(",".join(map(str, row)))


def d1001(path):
    """Summarize a D1001 SD battprofile.csv for the v4 regression. Columns:
    uptime_s,raw_ch2,cali_mv,batt_mv,batt_mv_smooth,usb_mv,vsys_pg,charging,soc,temp_dc,charge_en
    The discharge ended in a HARD SHUTDOWN, so uptime_s resets across boots -> segment by uptime reset,
    then report each session's on-wall/on-battery structure, V range, and any unplug transition (the
    base-frame 100% anchor = batt_mv the instant usb_mv drops)."""
    import csv as _csv
    rows = []
    with open(path) as f:
        for d in _csv.DictReader(f):
            try:
                rows.append({k: int(float(d[k])) for k in
                             ("uptime_s", "batt_mv", "batt_mv_smooth", "usb_mv", "charging", "soc", "charge_en")})
            except (ValueError, KeyError, TypeError):
                continue
    if not rows:
        print("no rows (check CSV header matches the expected columns)", file=sys.stderr)
        return
    USB_ON = 4000  # mV: usb_mv above this = wall power present
    # split into boot sessions on uptime reset
    sessions, cur = [], [rows[0]]
    for a, b in zip(rows, rows[1:]):
        if b["uptime_s"] >= a["uptime_s"]:
            cur.append(b)
        else:
            sessions.append(cur)
            cur = [b]
    sessions.append(cur)
    print(f"{len(rows)} rows, {len(sessions)} boot session(s)")
    for i, s in enumerate(sessions):
        wall = [r for r in s if r["usb_mv"] > USB_ON]
        batt = [r for r in s if r["usb_mv"] <= USB_ON]
        dur = (s[-1]["uptime_s"] - s[0]["uptime_s"]) / 60.0
        mv = [r["batt_mv"] for r in s]
        print(f"  session {i}: {len(s)} rows, {dur:.1f} min, batt_mv {min(mv)}..{max(mv)}, "
              f"soc {s[0]['soc']}->{s[-1]['soc']}, wall={len(wall)} batt={len(batt)} rows")
        # unplug transitions inside this session (wall -> battery): 100% anchor candidates
        for a, b in zip(s, s[1:]):
            if a["usb_mv"] > USB_ON and b["usb_mv"] <= USB_ON:
                print(f"    UNPLUG @ up={b['uptime_s']}s: batt_mv={b['batt_mv']} (smooth={b['batt_mv_smooth']}) "
                      f"= 100% anchor candidate")
    print("# next: pick the discharge session (long, on-battery, falling V) + its unplug anchor -> v4 fit")


def main():
    ap = argparse.ArgumentParser(description="E1001/D1001 battery-profiler analysis")
    ap.add_argument("cmd", choices=["fit", "summary", "d1001"])
    ap.add_argument("path")
    ap.add_argument("--csv")
    a = ap.parse_args()
    if a.cmd == "summary":
        summary(a.path)
    elif a.cmd == "d1001":
        d1001(a.path)
    else:
        fit(a.path, a.csv)


if __name__ == "__main__":
    main()
