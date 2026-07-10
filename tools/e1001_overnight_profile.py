#!/usr/bin/env python3
"""
E1001 overnight battery characterization — offsets + firmware profiler cycle, fully over MQTT.

Produces, unattended overnight, everything the panel's battery gauge needs:
  1. USB / charging voltage OFFSETS (off_usb_mv, off_charging_mv) — the step-each-knob measurement
     (ADR-0024 §3), automated via the panel's own charger switches (Charger HIZ replaces the physical
     USB unplug; no external plug, no human).
  2. A fresh V->SoC LUT + OCV anchors + a real mAh capacity / battery-life number — from the firmware's
     self-managed discharge->rest->charge->rest profiler cycle.
Morning: fit the LUT, bake in the offsets, push a v2 profile to the panel (no reflash).

WHY THIS IS SAFE TO RUN UNATTENDED (all firmware-side, verified in e1001.yaml):
  * The charger IC has a ~40s hardware watchdog. During a cycle the firmware kicks it every 2s; if the
    firmware hangs / this script dies / the network drops / the host reboots, the charger AUTO-REVERTS to
    default (charging) within ~40s. Hardware dead-man, independent of WiFi/MQTT/this script. (Validated on
    the panel by the `Prof Sim Crash` control.)
  * The discharge floor-gate stops discharge at `Prof V Floor` (we set 3.70 V ~= 20% SoC, well above the
    3.45 V brown-out). The external drain load only runs in DISCHARGE (can't over-drain at rest). On cycle
    completion the firmware sets HIZ off + charge on (safe).
This script adds belt-and-suspenders: a voltage watchdog (abort to safe if V dips below a hard floor or
telemetry goes dark) and a `finally:` that always issues `Prof Stop` (-> IDLE safe: HIZ off, charge on).

RUN IT ON ha-2 (the box E1001's broker lives on) against localhost for the most reliable safety loop:
    cd ~/home_automation
    venv/bin/python tools/e1001_overnight_profile.py --broker 127.0.0.1 --run 2>&1 | tee -a ~/e1001-profile-run.log

Topics (verified, device_name=e1001-c-office):
  telem : e1001-c-office/battprofile (5s JSON {st,v,hiz,chg,pg,mah,cyc,ocv_d,ocv_c,floor})
          e1001-c-office/sensor/battery_voltage/state
  ctl   : .../button/prof_start_cycle/command "PRESS"   .../button/prof_stop/command "PRESS"
          .../number/prof_v_floor/command (V)   .../number/prof_num_cycles/command
          .../number/prof_rest_seconds/command  .../number/prof_load_level/command (0..3)
          .../switch/charger_hiz/command ON|OFF .../switch/charge_enable/command ON|OFF
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paho.mqtt.client as mqtt                          # noqa: E402
try:
    from server.util.mqtt_creds import apply_credentials  # noqa: E402
except Exception:                                         # pragma: no cover
    def apply_credentials(c): pass

NODE = "e1001-c-office"
T_BATTPROF = f"{NODE}/battprofile"
T_VOLT = f"{NODE}/sensor/battery_voltage/state"


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Panel:
    """Thin MQTT wrapper: tracks the latest battprofile, issues control commands."""

    def __init__(self, broker: str, port: int, jsonl: Path):
        self._c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        apply_credentials(self._c)
        self._c.on_connect = self._on_connect
        self._c.on_message = self._on_message
        self.last = None            # latest battprofile dict
        self.last_ts = 0.0          # monotonic time of last battprofile
        self._jsonl = jsonl
        self._lock = threading.Lock()
        self._c.connect(broker, port, keepalive=30)
        self._c.loop_start()

    def _on_connect(self, c, u, f, rc, props=None):
        c.subscribe([(T_BATTPROF, 0), (T_VOLT, 0)])

    def _on_message(self, c, u, msg):
        if msg.topic == T_BATTPROF:
            try:
                d = json.loads(msg.payload.decode())
            except ValueError:
                return
            with self._lock:
                self.last = d
                self.last_ts = time.monotonic()
            # capture in e1001_capture.sh format: "<epoch> <json>" (e1001_profile.py e1001lut fits this)
            with self._jsonl.open("a") as fh:
                fh.write(f"{int(time.time())} {msg.payload.decode()}\n")

    # --- control ---
    def _pub(self, topic: str, payload: str):
        self._c.publish(topic, payload, qos=1)

    def num(self, name: str, value):    self._pub(f"{NODE}/number/{name}/command", str(value))
    def switch(self, name: str, on: bool): self._pub(f"{NODE}/switch/{name}/command", "ON" if on else "OFF")
    def button(self, name: str):        self._pub(f"{NODE}/button/{name}/command", "PRESS")

    def stop_safe(self):
        """Prof Stop -> IDLE (HIZ off, charge on). Also explicitly assert the safe switch states."""
        self.button("prof_stop")
        time.sleep(0.5)
        self.switch("charger_hiz", False)
        self.switch("charge_enable", True)

    def voltage(self):
        with self._lock:
            return None if self.last is None else self.last.get("v")

    def state(self):
        with self._lock:
            return None if self.last is None else self.last.get("st")

    def wait_telemetry(self, timeout=30):
        t0 = time.monotonic()
        while self.last is None and time.monotonic() - t0 < timeout:
            time.sleep(0.5)
        return self.last is not None

    def close(self):
        self._c.loop_stop()
        self._c.disconnect()


def _median_v(panel: Panel, hold_s: float, label: str) -> float:
    """Sample battprofile.v for hold_s and return the median (robust to ripple)."""
    samples, t0 = [], time.monotonic()
    while time.monotonic() - t0 < hold_s:
        v = panel.voltage()
        if v:
            samples.append(v)
        time.sleep(2)
    med = statistics.median(samples) if samples else float("nan")
    print(f"  [{utc()}] {label:18} median V={med:.4f}  (n={len(samples)})")
    return med


def phase_a_offsets(panel: Panel, settle: float, base_settle: float) -> dict:
    """Step-each-knob offset capture (ADR-0024 §3). HIZ replaces the manual USB unplug.
       base=battery-only, usb=USB present not charging, charging=charging. e-ink -> display offset 0.

       NB: in IDLE (no profiler kick) the charger's ~40-50s hardware watchdog auto-clears HIZ back to USB
       (verified live) — a safety dead-man. So the HIZ-on 'base' sample must stay UNDER ~40s (base_settle);
       the USB/charging steps hold HIZ off, so they're unaffected and can settle the full `settle`."""
    print(f"\n=== PHASE A: offset capture (base<{base_settle:.0f}s, usb/chg {settle:.0f}s each) ===")
    # base: battery-only (HIZ on, no charge, no external load) — MUST finish before the ~40s WD auto-clear
    panel.num("prof_load_level", 0)
    panel.switch("charge_enable", False)
    panel.switch("charger_hiz", True)
    time.sleep(4)                                     # brief settle, then sample well under the WD window
    v_base = _median_v(panel, base_settle, "base(battery)")
    # usb present, not charging
    panel.switch("charger_hiz", False)
    panel.switch("charge_enable", False)
    time.sleep(8)
    v_usb = _median_v(panel, settle, "usb(no charge)")
    # charging
    panel.switch("charge_enable", True)
    time.sleep(8)
    v_chg = _median_v(panel, settle, "charging")
    # restore safe
    panel.switch("charger_hiz", False)
    panel.switch("charge_enable", True)
    off = {
        "off_display_off_mv": 0,                      # e-ink: base frame is display-on; no display offset
        "off_usb_mv": int(round((v_usb - v_base) * 1000)),
        "off_charging_mv": int(round((v_chg - v_base) * 1000)),
        "_base_v": round(v_base, 4), "_usb_v": round(v_usb, 4), "_chg_v": round(v_chg, 4),
        "_soc_hint_v": round(v_base, 4), "measured_at": utc(),
    }
    print(f"  -> off_usb_mv={off['off_usb_mv']}  off_charging_mv={off['off_charging_mv']}")
    return off


def phase_b_cycle(panel: Panel, floor_v: float, cycles: int, rest_s: int, load: int,
                  hard_floor_v: float, max_hours: float) -> str:
    """Configure + trigger the firmware profiler cycle, then supervise with a voltage watchdog.
       Returns 'complete' | 'aborted:<why>'. The firmware's own 40s charger watchdog is the real
       safety; this loop is a redundant guard."""
    print(f"\n=== PHASE B: profiler cycle (floor={floor_v}V load={load} rest={rest_s}s cycles={cycles}) ===")
    panel.num("prof_v_floor", floor_v)
    panel.num("prof_num_cycles", cycles)
    panel.num("prof_rest_seconds", rest_s)
    panel.num("prof_load_level", load)
    time.sleep(2)
    panel.button("prof_start_cycle")
    print(f"  [{utc()}] cycle started")

    t0 = time.monotonic()
    seen_active = False
    while True:
        time.sleep(15)
        st, v = panel.state(), panel.voltage()
        age = time.monotonic() - panel.last_ts if panel.last_ts else 999
        elapsed_h = (time.monotonic() - t0) / 3600
        print(f"  [{utc()}] st={st} v={v} mah={panel.last.get('mah') if panel.last else '?'} "
              f"ocv_d={panel.last.get('ocv_d') if panel.last else '?'} "
              f"ocv_c={panel.last.get('ocv_c') if panel.last else '?'} elapsed={elapsed_h:.1f}h")
        if st is not None and st != 0:
            seen_active = True
        # --- redundant safety guards (firmware 40s HW watchdog is primary) ---
        if v is not None and v < hard_floor_v:
            return f"aborted:hard_floor({v}<{hard_floor_v})"
        if age > 180:
            return "aborted:telemetry_dark"
        if elapsed_h > max_hours:
            return "aborted:time_cap"
        # completion: was active, now back to IDLE with a full OCV anchor recorded
        if seen_active and st == 0 and panel.last and panel.last.get("ocv_c", 0) > 0:
            return "complete"


def main() -> int:
    ap = argparse.ArgumentParser(description="E1001 overnight battery profile + offset run")
    ap.add_argument("--broker", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--floor-v", type=float, default=3.70, help="discharge stop (~20%% SoC; >> 3.45 brownout)")
    ap.add_argument("--hard-floor-v", type=float, default=3.62, help="redundant abort-to-safe floor")
    ap.add_argument("--cycles", type=int, default=1)
    ap.add_argument("--rest-s", type=int, default=120)
    ap.add_argument("--load", type=int, default=2, choices=[0, 1, 2, 3],
                    help="discharge load 0..3; 2 = WiFi-PA drain so a full cycle finishes overnight")
    ap.add_argument("--settle", type=float, default=120, help="usb/charging offset step settle seconds")
    ap.add_argument("--base-settle", type=float, default=30,
                    help="battery-only (HIZ-on) sample seconds — MUST stay < the ~40s IDLE WD auto-clear")
    ap.add_argument("--max-hours", type=float, default=9.0)
    ap.add_argument("--out-dir", default=str(Path.home()), help="where to write the capture + offsets")
    ap.add_argument("--skip-offsets", action="store_true")
    ap.add_argument("--offsets-only", action="store_true",
                    help="measure offsets at the CURRENT SoC and exit (do at mid-SoC — charging draws real current)")
    ap.add_argument("--run", action="store_true", help="required: actually drive the battery")
    args = ap.parse_args()
    if not args.run:
        print("refusing to run without --run (this drives a real battery cycle)")
        return 2

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out_dir)
    jsonl = out / f"e1001-profile-{stamp}.jsonl"
    offj = out / f"e1001-offsets-{stamp}.json"
    print(f"capture -> {jsonl}\noffsets -> {offj}")

    panel = Panel(args.broker, args.port, jsonl)
    result = {"stamp": stamp, "capture": str(jsonl)}

    def _panic(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _panic)

    try:
        if not panel.wait_telemetry(30):
            print("NO battprofile telemetry — is e1001-c-office online on this broker? aborting.")
            return 1
        print(f"panel online: v={panel.voltage()} st={panel.state()}")
        if not args.skip_offsets:
            result["offsets"] = phase_a_offsets(panel, args.settle, args.base_settle)
            offj.write_text(json.dumps(result["offsets"], indent=2))
        if args.offsets_only:
            print("\n=== offsets-only: done (panel left safe) ===")
            return 0
        result["cycle"] = phase_b_cycle(panel, args.floor_v, args.cycles, args.rest_s, args.load,
                                        args.hard_floor_v, args.max_hours)
        print(f"\n=== RESULT: {result['cycle']} ===")
        result["final"] = panel.last
        offj.write_text(json.dumps(result, indent=2))
        return 0 if result["cycle"] == "complete" else 3
    except KeyboardInterrupt:
        print("\n!! interrupted — forcing panel to SAFE state")
        return 130
    finally:
        try:
            panel.stop_safe()                            # ALWAYS return the panel to safe (HIZ off, charge on)
            time.sleep(1)
            print(f"[{utc()}] panel set SAFE (Prof Stop). final v={panel.voltage()}")
        finally:
            panel.close()


if __name__ == "__main__":
    raise SystemExit(main())
