#!/usr/bin/env python3
"""Layer-1 power/idle sampler (docs/power-optimization.md §3.1).

Fires from a systemd timer every ~5 min and appends ONE CSV row of DELTAS of cumulative kernel counters.
Counters are free to read; the delta over the interval gives the interval's behaviour with no continuous
polling — so the profiler's own footprint is ~one wake + a handful of reads per interval (and it records
that footprint in the `sampler_ms` column so the campaign can subtract it).

Reads (root, for RAPL energy_uj which is 0400):
  - RAPL package energy        -> avg package watts for the interval
  - per-CPU cpuidle state time  -> %C1 / %C2 residency (the idle KPI)
  - /proc/stat                  -> CPU busy %
  - /proc/interrupts            -> per-source IRQ rate (LOC/CAL/NIC/xhci/RES)
  - /proc/loadavg, hwmon temp, top-1 process by CPU

State (previous cumulative readings) is kept in STATE_PATH; on first run or after a reboot (any monotonic
counter goes backwards) it re-baselines and writes a marker row instead of bogus deltas. Pure stdlib so it
needs no venv and can run before any package is installed.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import time

OUT_DIR = os.environ.get("HA_POWER_DIR", "/var/log/ha-power")
CSV_PATH = os.path.join(OUT_DIR, "samples.csv")
STATE_PATH = os.path.join(OUT_DIR, "state.json")
EVENTS_PATH = os.path.join(OUT_DIR, "events.log")
# Spike-attribution thresholds: an interval over EITHER captures what was running (feeds the emergent-event
# hunt + the recompile-candidate shortlist — see docs §2.7). Tunable; defaults sized to idle ~6W / ~4% busy.
SPIKE_BUSY_PCT = float(os.environ.get("HA_POWER_BUSY_PCT", "50"))
SPIKE_PKG_W = float(os.environ.get("HA_POWER_W", "15"))
RAPL = "/sys/class/powercap/intel-rapl:0"
IRQS = {"LOC": "irq_loc", "CAL": "irq_cal", "RES": "irq_res"}   # plus NIC + xhci matched by substring
COLUMNS = ["ts", "interval_s", "pkg_w", "c1_pct", "c2_pct", "cpu_busy_pct", "load1", "temp_c",
           "irq_loc_ps", "irq_cal_ps", "irq_res_ps", "irq_nic_ps", "irq_xhci_ps",
           "top_cmd", "top_cpu_pct", "sampler_ms", "note"]


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def rapl_energy():
    e = _read(os.path.join(RAPL, "energy_uj"))
    mx = _read(os.path.join(RAPL, "max_energy_range_uj"))
    return (int(e) if e else None, int(mx) if mx else None)


def cstate_times():
    """Aggregate cpuidle residency (us) summed across CPUs, by state NAME (POLL/C1/C2)."""
    out, ncpu = {}, 0
    for cpu in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpuidle"):
        ncpu += 1
        for st in glob.glob(os.path.join(cpu, "state*")):
            name = _read(os.path.join(st, "name")) or "?"
            t = _read(os.path.join(st, "time"))
            if t:
                out[name] = out.get(name, 0) + int(t)
    return out, ncpu


def cpu_stat():
    line = (_read("/proc/stat") or "cpu").split("\n")[0].split()[1:]
    vals = [int(x) for x in line]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)   # idle + iowait
    return sum(vals), idle


def interrupts():
    out = {"irq_nic": 0, "irq_xhci": 0}
    for k in IRQS.values():
        out[k] = 0
    txt = _read("/proc/interrupts") or ""
    for line in txt.splitlines():
        parts = line.split()
        if not parts:
            continue
        tag = parts[0].rstrip(":")
        # numeric IRQ lines: sum the per-CPU counts (the leading numeric columns)
        nums, rest = [], []
        for p in parts[1:]:
            if p.isdigit():
                nums.append(int(p))
            else:
                rest.append(p)
        total = sum(nums)
        if tag in IRQS:
            out[IRQS[tag]] += total
        desc = " ".join(rest).lower()
        if "enp" in desc or "eth" in desc:
            out["irq_nic"] += total
        if "xhci" in desc:
            out["irq_xhci"] += total
    return out


def temp_c():
    best = None
    for f in glob.glob("/sys/class/hwmon/hwmon*/temp*_input"):
        v = _read(f)
        if v:
            c = int(v) / 1000.0
            best = c if best is None else max(best, c)
    if best is None:
        for f in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
            v = _read(f)
            if v:
                c = int(v) / 1000.0
                best = c if best is None else max(best, c)
    return best


def top_proc():
    """Top process by CPU, excluding this sampler's own pid + the ps snapshot (so we don't just report
    ourselves). Returns the highest-CPU OTHER process."""
    me = os.getpid()
    try:
        r = subprocess.run(["ps", "-eo", "pid,pcpu,comm", "--sort=-pcpu", "--no-headers"],
                           capture_output=True, text=True, timeout=5)
        for ln in r.stdout.splitlines():
            parts = ln.split(None, 2)
            if len(parts) < 3:
                continue
            pid, pc, cmd = parts
            if pid.isdigit() and int(pid) == me:      # skip ourselves
                continue
            if cmd.strip() == "ps":                    # skip the snapshot tool
                continue
            return cmd.strip(), float(pc)
    except Exception:
        pass
    return "-", 0.0


def capture_spike(reason: str, metrics: dict):
    """Append a what-was-running snapshot when an interval is unusually busy/hot. The top-CPU processes
    here are the raw material for the recompile-candidate shortlist (compute-bound + not dispatched)."""
    try:
        r = subprocess.run(["ps", "-eo", "pcpu,pmem,comm", "--sort=-pcpu", "--no-headers"],
                           capture_output=True, text=True, timeout=5)
        top = "; ".join(ln.strip() for ln in r.stdout.splitlines()[:8])
    except Exception:
        top = "(ps failed)"
    line = (f"{metrics.get('ts')} SPIKE [{reason}] busy={metrics.get('cpu_busy_pct')}% "
            f"pkgW={metrics.get('pkg_w')} c2={metrics.get('c2_pct')}% load1={metrics.get('load1')} "
            f"irq_cal/s={metrics.get('irq_cal_ps')} | top: {top}\n")
    with open(EVENTS_PATH, "a") as f:
        f.write(line)


def main():
    t0 = time.time()
    os.makedirs(OUT_DIR, exist_ok=True)
    now = time.time()
    energy, emax = rapl_energy()
    cst, ncpu = cstate_times()
    stot, sidle = cpu_stat()
    irq = interrupts()
    load1 = (_read("/proc/loadavg") or "0").split()[0]
    temp = temp_c()
    top_cmd, top_cpu = top_proc()

    cur = {"wall": now, "energy": energy, "cst": cst, "stot": stot, "sidle": sidle, "irq": irq}
    prev = None
    try:
        with open(STATE_PATH) as f:
            prev = json.load(f)
    except (OSError, ValueError):
        prev = None

    def row(vals, note):
        ms = round((time.time() - t0) * 1000, 1)
        vals = dict(vals); vals["sampler_ms"] = ms; vals["note"] = note
        line = ",".join(str(vals.get(c, "")) for c in COLUMNS)
        new = not os.path.exists(CSV_PATH)
        with open(CSV_PATH, "a") as f:
            if new:
                f.write(",".join(COLUMNS) + "\n")
            f.write(line + "\n")

    base = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "load1": load1,
            "temp_c": round(temp, 1) if temp else "", "top_cmd": top_cmd, "top_cpu_pct": top_cpu}

    # First run, or a counter went backwards (reboot / wrap on a non-RAPL counter) -> re-baseline.
    reset = (prev is None or now <= prev.get("wall", 0)
             or stot < prev.get("stot", 0)
             or cst.get("C2", 0) < (prev.get("cst") or {}).get("C2", 0))
    if reset:
        row(base, "baseline" if prev is None else "rebaseline(reboot?)")
    else:
        dt = now - prev["wall"]
        # RAPL: handle wrap
        pw = ""
        if energy is not None and prev.get("energy") is not None and emax:
            de = energy - prev["energy"]
            if de < 0:
                de += emax
            pw = round(de / 1e6 / dt, 2)
        def cdelta(name):
            return cst.get(name, 0) - (prev.get("cst") or {}).get(name, 0)
        denom = ncpu * dt * 1e6 if ncpu and dt else 0
        c1 = round(cdelta("C1") / denom * 100, 1) if denom else ""
        c2 = round(cdelta("C2") / denom * 100, 1) if denom else ""
        dtot = stot - prev["stot"]; didle = sidle - prev["sidle"]
        busy = round((dtot - didle) / dtot * 100, 1) if dtot > 0 else ""
        def ips(k):
            return round((irq[k] - prev["irq"].get(k, 0)) / dt, 1) if dt else ""
        base.update({"interval_s": round(dt, 1), "pkg_w": pw, "c1_pct": c1, "c2_pct": c2,
                     "cpu_busy_pct": busy, "irq_loc_ps": ips("irq_loc"), "irq_cal_ps": ips("irq_cal"),
                     "irq_res_ps": ips("irq_res"), "irq_nic_ps": ips("irq_nic"),
                     "irq_xhci_ps": ips("irq_xhci")})
        try:                                  # spike-attribution: capture what ran if unusually busy/hot
            spk = []
            if busy != "" and float(busy) >= SPIKE_BUSY_PCT:
                spk.append(f"busy>={SPIKE_BUSY_PCT}")
            if pw != "" and float(pw) >= SPIKE_PKG_W:
                spk.append(f"pkgW>={SPIKE_PKG_W}")
            if spk:
                capture_spike(",".join(spk), base)
        except (TypeError, ValueError):
            pass
        row(base, "")

    with open(STATE_PATH, "w") as f:
        json.dump(cur, f)


if __name__ == "__main__":
    main()
