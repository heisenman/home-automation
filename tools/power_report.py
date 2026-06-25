#!/usr/bin/env python3
"""Summarize the power/idle campaign (docs/power-optimization.md). Reads the Layer-1 sampler CSV +
spike events.log (+ best-effort `acct` top-CPU via `sa`), and prints active-vs-idle residency, package
watts (mean/p95), IRQ-rate trends, and the emergent-spike list — the day-7 exit-criterion report, also
runnable any time for an interim readout. Read-only; no system impact.

  python3 tools/power_report.py            # default /var/log/ha-power
  python3 tools/power_report.py --dir DIR
"""
from __future__ import annotations

import argparse
import os
import subprocess


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def pctl(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.environ.get("HA_POWER_DIR", "/var/log/ha-power"))
    ap.add_argument("--idle-busy", type=float, default=10.0, help="busy%% below this = 'idle' bucket")
    a = ap.parse_args()
    csv = os.path.join(a.dir, "samples.csv")
    try:
        with open(csv) as f:
            lines = f.read().splitlines()
    except OSError as e:
        print(f"cannot read {csv}: {e} (try sudo, or --dir)")
        return
    if len(lines) < 2:
        print("no samples yet")
        return
    cols = lines[0].split(",")
    idx = {c: i for i, c in enumerate(cols)}
    rows = [ln.split(",") for ln in lines[1:] if ln]

    def col(r, name):
        return _num(r[idx[name]]) if name in idx and idx[name] < len(r) else None

    delta = [r for r in rows if (r[idx["note"]] if idx.get("note", 99) < len(r) else "") == ""]
    busy = [(r, col(r, "cpu_busy_pct")) for r in delta]
    busy = [(r, b) for r, b in busy if b is not None]
    idle = [r for r, b in busy if b < a.idle_busy]
    active = [r for r, b in busy if b >= a.idle_busy]
    watts = [w for w in (col(r, "pkg_w") for r in delta) if w is not None]
    c2_idle = [c for c in (col(r, "c2_pct") for r in idle) if c is not None]
    c2_active = [c for c in (col(r, "c2_pct") for r in active) if c is not None]
    cal = [(col(r, "irq_cal_ps"), r[idx["ts"]]) for r in delta if col(r, "irq_cal_ps") is not None]

    span = f"{rows[0][idx['ts']]} → {rows[-1][idx['ts']]}"
    print(f"== Power campaign report ==  ({len(rows)} samples; {span})")
    print(f"  buckets: {len(idle)} idle (<{a.idle_busy}% busy) / {len(active)} active")
    if c2_idle:
        print(f"  C2 residency: idle mean {sum(c2_idle)/len(c2_idle):.1f}%"
              + (f" · active mean {sum(c2_active)/len(c2_active):.1f}%" if c2_active else ""))
    if watts:
        print(f"  package watts: mean {sum(watts)/len(watts):.2f} W · p95 {pctl(watts,95):.2f} W · "
              f"min {min(watts):.2f} · max {max(watts):.2f}")
    if cal:
        mx = max(cal, key=lambda t: t[0])
        print(f"  CAL/IPI rate: mean {sum(c for c,_ in cal)/len(cal):.0f}/s · peak {mx[0]:.0f}/s @ {mx[1]}")

    ev = os.path.join(a.dir, "events.log")
    try:
        elines = [l for l in open(ev).read().splitlines() if l.strip()]
    except OSError:
        elines = []
    print(f"\n  emergent spikes (events.log): {len(elines)}")
    for l in elines[-5:]:
        print("   ", l[:160])

    print("\n  recompile-candidate check (docs §2.7): compute-bound + NOT runtime-dispatched only.")
    try:
        r = subprocess.run(["sudo", "-n", "sa", "--percentages"], capture_output=True, text=True, timeout=8)
        out = [l for l in (r.stdout or "").splitlines() if l.strip()]
        if out and "Usage:" not in out[0]:
            print("  top commands by CPU (sudo sa --percentages):")
            for l in out[:8]:
                print("   ", l.strip())
        else:
            print("  (run `sudo sa --percentages` for per-command CPU totals)")
    except Exception:
        print("  (acct summary needs `sudo sa`; skipped)")


if __name__ == "__main__":
    main()
