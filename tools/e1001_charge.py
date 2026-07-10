#!/usr/bin/env python3
"""E1001 charge-up tool — assert charging, VERIFY the cell is actually climbing, optionally chain the profiler.

A small code-driven tool (per the "long tests must be code-driven" directive): it puts the panel into a known
CHARGING state and confirms — with real telemetry — that the voltage is rising / the charger reports charging,
rather than assuming. When the cell reaches a reasonable charged threshold it can AUTO-START the battery
profiler so the whole charge->profile arc runs unattended and session-independent.

It drives the panel over MQTT via the same Panel wrapper the profiler uses (charger_hiz OFF + charge_enable ON
+ prof load 0 + Prof Stop -> firmware IDLE = charging). The firmware's ~40s charger hardware watchdog means the
worst case if this tool dies is the panel reverts to charging anyway — safe.

Typical uses:
    # just make sure it's charging and report once (validation; no wait) — the "make sure it's charging" ask
    venv/bin/python tools/e1001_charge.py --once

    # charge to ~4.10 V then automatically start the profiler service (the full unattended chain)
    venv/bin/python tools/e1001_charge.py --until-mv 4100 --then-profile

The profiler it chains to is tools/e1001_profile_run.py, run as the detached oneshot ha-e1001-profile.service
(separate systemd unit = its own cgroup = survives this tool exiting). See systemd/ha-e1001-charge.service.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.e1001_overnight_profile import Panel, NODE, utc  # noqa: E402  (reuse the proven MQTT wrapper)

PROFILE_UNIT = "ha-e1001-profile.service"


def _mv(v):
    """battprofile.v is volts; return millivolts int or None."""
    return None if v is None else int(round(v * 1000))


def ensure_charging(panel: Panel):
    """Put the panel into the known charging/IDLE state: Prof Stop, HIZ off, charge on, no load."""
    panel.num("prof_load_level", 0)
    panel.button("prof_stop")
    time.sleep(0.5)
    panel.switch("charger_hiz", False)
    panel.switch("charge_enable", True)


def launch_profiler(unit: str) -> bool:
    """Start the profiler as a SEPARATE systemd unit (--no-block so we don't wait out the whole run).
       Separate unit => own cgroup => independent of this tool's lifecycle. Falls back to sudo -n."""
    for cmd in (["systemctl", "start", "--no-block", unit],
                ["sudo", "-n", "systemctl", "start", "--no-block", unit]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                print(f"[{utc()}] started {unit} via: {' '.join(cmd)}")
                return True
        except Exception as e:  # noqa: BLE001
            print(f"[{utc()}] {' '.join(cmd)} -> {e}", file=sys.stderr)
    print(f"[{utc()}] !! could not start {unit} (is it installed? try: sudo systemctl start {unit})",
          file=sys.stderr)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="E1001 charge-up + optional profiler chain")
    ap.add_argument("--broker", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--until-mv", type=int, default=4100,
                    help="reasonable charged threshold to consider the cell ready for a profile run "
                         "(near-full so the discharge LUT covers the whole range)")
    ap.add_argument("--topped-mv", type=int, default=4050,
                    help="if the charger reports DONE (charging OFF) at/above this, treat as charged")
    ap.add_argument("--timeout-min", type=float, default=240.0, help="give up waiting (charge-from-empty ~2-3h)")
    ap.add_argument("--poll-s", type=float, default=30.0)
    ap.add_argument("--once", action="store_true",
                    help="assert charging + report a single telemetry snapshot, then exit (validation mode)")
    ap.add_argument("--then-profile", action="store_true",
                    help=f"on reaching --until-mv, start {PROFILE_UNIT} (the detached profiler)")
    ap.add_argument("--profile-unit", default=PROFILE_UNIT)
    args = ap.parse_args()

    # charge tool never writes a capture; give Panel a throwaway sink
    sink = Path("/dev/null")
    panel = Panel(args.broker, args.port, sink)
    try:
        if not panel.wait_telemetry(30):
            print(f"NO battprofile telemetry from {NODE} on {args.broker}:{args.port} — is the panel online? "
                  "aborting.", file=sys.stderr)
            return 1
        ensure_charging(panel)
        time.sleep(3)
        v0 = panel.voltage()
        d0 = panel.last or {}
        print(f"[{utc()}] {NODE}: v={v0} ({_mv(v0)} mV) st={d0.get('st')} chg={d0.get('chg')} "
              f"hiz={d0.get('hiz')} pg={d0.get('pg')}")

        def charged(v, d):
            mv = _mv(v)
            if mv is None:
                return False
            if mv >= args.until_mv:
                return True
            # charger 'done' (not charging) while already high == topped off / tapered
            return (not d.get("chg")) and mv >= args.topped_mv

        if args.once:
            state = "CHARGED" if charged(v0, d0) else "charging"
            print(f"[{utc()}] charging asserted; state={state} "
                  f"(threshold {args.until_mv} mV). --once: not waiting.")
            return 0

        if charged(v0, d0):
            print(f"[{utc()}] already at/above {args.until_mv} mV — charged.")
        else:
            # wait, confirming the cell actually climbs (else the charger isn't working)
            t0 = time.monotonic()
            last_mv, stalled = _mv(v0), 0
            while True:
                time.sleep(args.poll_s)
                v, d = panel.voltage(), (panel.last or {})
                mv = _mv(v)
                elapsed_m = (time.monotonic() - t0) / 60.0
                print(f"[{utc()}] v={v} ({mv} mV) chg={d.get('chg')} pg={d.get('pg')} "
                      f"elapsed={elapsed_m:.1f}m")
                if charged(v, d):
                    print(f"[{utc()}] reached {args.until_mv} mV — charged.")
                    break
                # "make sure it's charging up": if not rising AND charger not charging, it's not charging
                if mv is not None and last_mv is not None:
                    if mv <= last_mv and not d.get("chg"):
                        stalled += 1
                        if stalled >= 3:
                            print(f"[{utc()}] !! voltage not rising ({mv} mV) and charger not charging — "
                                  "charger may be disabled/faulted. Not proceeding to profile.", file=sys.stderr)
                            return 2
                    else:
                        stalled = 0
                    last_mv = max(mv, last_mv)
                if elapsed_m > args.timeout_min:
                    print(f"[{utc()}] !! timeout after {args.timeout_min:.0f} min at {mv} mV "
                          f"(< {args.until_mv}). Panel left charging.", file=sys.stderr)
                    return 3

        if args.then_profile:
            ensure_charging(panel)  # leave it safe/charging before the profiler takes over
            time.sleep(1)
            return 0 if launch_profiler(args.profile_unit) else 4
        return 0
    finally:
        panel.close()


if __name__ == "__main__":
    raise SystemExit(main())
