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


USB_ON = 4000  # mV: usb_mv above this = wall power present


def _d1001_sessions(path):
    """Parse the D1001 SD battprofile.csv into boot sessions (uptime_s resets across the hard-shutdown)."""
    import csv as _csv
    rows = []
    with open(path) as f:
        for d in _csv.DictReader(f):
            try:
                rows.append({k: int(float(d[k])) for k in
                             ("uptime_s", "batt_mv", "batt_mv_smooth", "usb_mv", "charging", "soc", "charge_en")})
            except (ValueError, KeyError, TypeError):
                continue
    sessions, cur = [], [rows[0]] if rows else []
    for a, b in zip(rows, rows[1:]):
        (cur.append(b) if b["uptime_s"] >= a["uptime_s"] else (sessions.append(cur), cur := [b]))
    if cur:
        sessions.append(cur)
    return rows, sessions


def _binned_lut(samples, n=21):
    """samples = [(soc_ref_pct, v_mv)]; return an n-point LUT (index i -> SoC = 100*i/(n-1)) = the
    windowed-median V at each grid SoC, empty windows linearly interpolated, forced strictly ascending
    (SoC never inverts). Median is robust to the on-battery voltage ripple. The two ENDPOINTS use a
    narrow (<=1%) one-sided window so they land on the true anchor voltage: a symmetric +-2.5% window
    at a grid extreme spans a steep dV/dSoC segment and would drag the 100%/0% value inward (the top
    anchor is the settled unplug reading, not the average over the top 2.5%)."""
    from statistics import median
    step = 100.0 / (n - 1)
    half = step / 2.0
    lut, counts = [None] * n, [0] * n
    for i in range(n):
        g = i * step
        lo, hi = (g - half, g + half)
        if i == 0:       lo, hi = -0.1, 1.0        # 0%   = shutdown floor (narrow)
        elif i == n - 1: lo, hi = 99.0, 100.1      # 100% = settled unplug voltage (narrow)
        vs = [v for soc, v in samples if lo <= soc <= hi]
        counts[i] = len(vs)
        lut[i] = int(median(vs)) if vs else None
    for i in range(n):                                   # fill empty windows by interpolating neighbors
        if lut[i] is None:
            lo = next((j for j in range(i, -1, -1) if lut[j] is not None), None)
            hi = next((j for j in range(i, n) if lut[j] is not None), None)
            if lo is None:  lut[i] = lut[hi]
            elif hi is None: lut[i] = lut[lo]
            else: lut[i] = int(lut[lo] + (lut[hi] - lut[lo]) * (i - lo) / (hi - lo))
    for i in range(1, n):                                # strictly monotonic ascending
        if lut[i] <= lut[i - 1]:
            lut[i] = lut[i - 1] + 1
    return lut, counts


def d1001(path, csv_out=None):
    """Summarize a D1001 SD battprofile.csv AND emit the v4 V->SoC LUT. Columns:
    uptime_s,raw_ch2,cali_mv,batt_mv,batt_mv_smooth,usb_mv,vsys_pg,charging,soc,temp_dc,charge_en
    Auto-picks the discharge session (the one with the most on-battery rows), anchors 100% at the
    unplug and 0% at the shutdown floor, and — under the constant-load assumption (coulombs ~ time) —
    builds a base-frame (on-battery/display-on/not-charging = no offsets to remove) LUT. Cross-checks
    the recharge leg for hysteresis."""
    rows, sessions = _d1001_sessions(path)
    if not rows:
        print("no rows (check CSV header matches the expected columns)", file=sys.stderr)
        return
    print(f"{len(rows)} rows, {len(sessions)} boot session(s)")
    for i, s in enumerate(sessions):
        batt = [r for r in s if r["usb_mv"] <= USB_ON]
        dur = (s[-1]["uptime_s"] - s[0]["uptime_s"]) / 60.0
        mv = [r["batt_mv"] for r in s]
        tag = "  <- discharge" if len(batt) > 100 and s[0]["soc"] - s[-1]["soc"] > 30 else ""
        print(f"  session {i:2}: {len(s):4} rows, {dur:6.1f} min, batt_mv {min(mv)}..{max(mv)}, "
              f"soc {s[0]['soc']:3}->{s[-1]['soc']:3}, batt={len(batt)}{tag}")

    # --- pick the discharge session: most on-battery rows, SoC clearly falling ---
    def batt_rows(s): return [r for r in s if r["usb_mv"] <= USB_ON]
    disch = max(sessions, key=lambda s: len(batt_rows(s)) if s[0]["soc"] - s[-1]["soc"] > 30 else 0)
    d = batt_rows(disch)
    if len(d) < 50:
        print("\n# no usable discharge session (need a long on-battery run falling to the floor)", file=sys.stderr)
        return
    t0, t1 = d[0]["uptime_s"], d[-1]["uptime_s"]
    span = max(t1 - t0, 1)
    # base frame = the on-battery loaded reading; raw batt_mv (smooth lags through the unplug transient)
    samples = [(100.0 * (1.0 - (r["uptime_s"] - t0) / span), r["batt_mv"]) for r in d]
    lut, counts = _binned_lut(samples, 21)
    v100, v0 = lut[-1], lut[0]
    dur_min = span / 60.0
    print(f"\n# DISCHARGE fit: {len(d)} on-battery samples over {dur_min:.1f} min  "
          f"({dur_min * 60 / len(d):.1f}s cadence)")
    print(f"# anchors: 100% (unplug, base-frame loaded) = {v100} mV   0% (shutdown floor) = {v0} mV")
    thin = [i for i, c in enumerate(counts) if c < 3]
    if thin:
        print(f"# NOTE thin SoC bins (<3 samples, interpolated): {[i * 5 for i in thin]}%")

    # --- hysteresis cross-check vs the recharge leg (on-wall, charging; time-linear, crude) ---
    chg = max(sessions, key=lambda s: len(s) if (s[-1]["soc"] - s[0]["soc"] > 30
                                                 and sum(r["charging"] for r in s) > 10) else 0)
    if chg is not disch and chg[-1]["soc"] - chg[0]["soc"] > 30:
        c0, c1 = chg[0]["uptime_s"], chg[-1]["uptime_s"]
        cspan = max(c1 - c0, 1)
        csamp = [(100.0 * (r["uptime_s"] - c0) / cspan, r["batt_mv"]) for r in chg]
        clut, _ = _binned_lut(csamp, 21)
        gap50 = clut[10] - lut[10]
        print(f"# hysteresis: charge-leg reads ~{gap50} mV above discharge at 50% "
              f"(charge terminal elevated; base-frame LUT correctly uses the discharge leg)")

    print("\n# v4 LUT (base-frame mV, ascending, 21 pts @ 5%/step) — paste into ha_batt_profile_d1001_default:")
    body = ",\n        ".join(", ".join(str(v) for v in lut[i:i + 11]) for i in range(0, 21, 11))
    print("    static const int lut21[21] = {\n        " + body + ",\n    };")
    if csv_out:
        with open(csv_out, "w") as f:
            f.write("soc_pct,v_mv\n")
            for i, v in enumerate(lut):
                f.write(f"{i * 5},{v}\n")
        print(f"# wrote 21-point LUT -> {csv_out}")


def main():
    ap = argparse.ArgumentParser(description="E1001/D1001 battery-profiler analysis")
    ap.add_argument("cmd", choices=["fit", "summary", "d1001"])
    ap.add_argument("path")
    ap.add_argument("--csv")
    a = ap.parse_args()
    if a.cmd == "summary":
        summary(a.path)
    elif a.cmd == "d1001":
        d1001(a.path, a.csv)
    else:
        fit(a.path, a.csv)


if __name__ == "__main__":
    main()
