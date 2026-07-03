#!/usr/bin/env python3
"""Battery characterization harness for the reTerminal D1001 (ADR-0024 §characterization).

Automates the *scriptable* knobs of the per-device characterization cycle and captures the
telemetry stream to CSV, so a full charge/discharge run is one repeatable command instead of a
hand-driven bench session. Emits a candidate profile (ha_batt_profile_t shape) at the end.

WHAT IS SCRIPTABLE (driven here, over MQTT):
  * charge enable/disable  -> cmd/charge {auto|hold|on|off|reset<ms>}   (BQ25616 /CE via expander P12)
  * display load           -> cmd/screen {on|off}                       (bsp_display sleep/wake)
  * sampling               -> we consume d1001-beachhead/battprofile as published
WHAT IS NOT (resolved 2026-07-03, see docs/design/battery-power-policy.md "Power topology"):
  * USB-input kill         -> NOT firmware-reachable on the D1001 (NVDC power-path internal to the
                              BQ; HIZ is I2C-only + charger not on I2C). The discharge leg needs a
                              human "unplug USB" step — this harness PAUSES for it, it does not fake it.

DEPENDENCIES: none beyond mosquitto_pub / mosquitto_sub (already on the bench). No paho.

OPEN METHOD QUESTIONS (for Hugh — the fit is deliberately a stub until we settle these):
  1. SoC axis: the panel has no current sense, so a discharge curve is V-vs-TIME, not V-vs-COULOMB.
     Time-based SoC is only honest at a ~constant load (display-on, USB-out). Do we (a) accept
     time-normalized SoC at fixed load as "good enough" for a wall panel, or (b) add a coulomb/rate
     estimate, or (c) drive a known external load for a controlled discharge?
  2. Sample rate: firmware SAMPLE_MS is fixed at 15 s. Offsets/transitions want 250 ms-1 s. That is a
     firmware hook (a cmd to set the cadence / a burst mode) — not yet built. This harness runs at
     whatever cadence the device publishes and flags the gap.
  3. Anchors: 100% needs the STAT charging->done edge with the display BLANK (load starves charge).
     This harness can hold the display off during the charge leg to catch it.
"""
import argparse
import json
import os
import subprocess
import sys
import time

BROKER = os.environ.get("HA_COORD_BROKER", "192.168.0.200")
PORT = int(os.environ.get("HA_COORD_PORT", "1883"))
BASE = "d1001-beachhead"
T_TELEM = f"{BASE}/battprofile"
T_CMD_CHARGE = f"{BASE}/cmd/charge"
T_CMD_SCREEN = f"{BASE}/cmd/screen"

# battprofile JSON fields, in CSV column order (see main/bat_profile.c).
FIELDS = ["up", "raw", "cali_mv", "batt_mv", "smooth_mv", "usb_mv", "pg", "chg", "soc", "temp_dc", "charge_en"]


class Mqtt:
    """Thin wrapper over the mosquitto CLI (no python deps). dry_run prints instead of sending."""

    def __init__(self, broker=BROKER, port=PORT, dry_run=False):
        self.broker, self.port, self.dry_run = broker, port, dry_run

    def pub(self, topic, payload):
        if self.dry_run:
            print(f"[dry-run] pub {topic} = {payload}")
            return
        subprocess.run(["mosquitto_pub", "-h", self.broker, "-p", str(self.port),
                        "-t", topic, "-m", str(payload)], check=True)

    def sub_stream(self, topic):
        """Yield payload strings from a live subscription (generator; blocks). Empty in dry-run."""
        if self.dry_run:
            return
        proc = subprocess.Popen(["mosquitto_sub", "-h", self.broker, "-p", str(self.port), "-t", topic],
                                stdout=subprocess.PIPE, text=True)
        try:
            for line in proc.stdout:
                yield line.strip()
        finally:
            proc.terminate()


class Recorder:
    """Streams telemetry to a CSV, tagging each row with the current phase label + wall clock."""

    def __init__(self, mqtt, out_path):
        self.mqtt, self.out_path = mqtt, out_path
        self.phase = "idle"
        self._fh = None

    def open(self):
        self._fh = open(self.out_path, "w")
        self._fh.write("wall_iso,phase," + ",".join(FIELDS) + "\n")
        self._fh.flush()

    def _write(self, obj):
        wall = time.strftime("%Y-%m-%dT%H:%M:%S")
        row = [wall, self.phase] + [str(obj.get(f, "")) for f in FIELDS]
        self._fh.write(",".join(row) + "\n")
        self._fh.flush()

    def collect(self, seconds, phase):
        """Log telemetry for `seconds`, tagging rows with `phase`. Returns the rows collected."""
        self.phase = phase
        rows, deadline = [], time.time() + seconds
        print(f"[{phase}] collecting for {seconds:.0f}s ...")
        if self.mqtt.dry_run:
            time.sleep(min(seconds, 0.1))
            return rows
        for payload in self.mqtt.sub_stream(T_TELEM):
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            self._write(obj)
            rows.append(obj)
            if time.time() >= deadline:
                break
        return rows


# --- knobs -------------------------------------------------------------------------------------

def set_charge(mqtt, mode):   mqtt.pub(T_CMD_CHARGE, mode)      # auto|hold|on|off
def set_screen(mqtt, on):     mqtt.pub(T_CMD_SCREEN, "on" if on else "off")


def prompt(msg):
    """Manual step (e.g. the un-scriptable USB unplug). Skipped in non-interactive runs."""
    if sys.stdin.isatty():
        input(f"\n>>> {msg}\n    press ENTER when done ... ")
    else:
        print(f"\n>>> MANUAL STEP (no TTY, continuing): {msg}\n")


# --- phases ------------------------------------------------------------------------------------

def measure_offsets(rec, mqtt, settle_s):
    """Step each knob one at a time and record the delta vs the base frame (display-on/battery/idle).
    Rigorous and fully scriptable: this is the part that does NOT need a fit."""
    print("\n=== OFFSET MEASUREMENT (base frame = display-on / on-battery / not-charging) ===")
    prompt("Ensure USB is UNPLUGGED and the cell is mid-range (not full, not near empty).")
    set_screen(mqtt, True); set_charge(mqtt, "hold")
    base = rec.collect(settle_s, "offset_base")
    set_screen(mqtt, False)
    disp_off = rec.collect(settle_s, "offset_display_off")
    set_screen(mqtt, True)
    prompt("PLUG IN USB now (to measure the USB-attached step).")
    usb_on = rec.collect(settle_s, "offset_usb_on")
    set_charge(mqtt, "on")
    charging = rec.collect(settle_s, "offset_charging")
    set_charge(mqtt, "hold")
    return {"base": base, "display_off": disp_off, "usb_on": usb_on, "charging": charging}


def discharge(rec, mqtt, minutes, floor_mv):
    """Fixed-load discharge (display on, USB out) down to a SAFE stop above the knee."""
    print("\n=== DISCHARGE (display on, USB out) ===")
    prompt("UNPLUG USB. Discharge runs at display-on load until the floor or the time cap.")
    set_screen(mqtt, True); set_charge(mqtt, "hold")
    rec.phase = "discharge"
    return rec.collect(minutes * 60, "discharge")  # honest stop is floor_mv; caller watches the CSV


def charge_to_top(rec, mqtt, minutes, blank_display):
    """Charge leg. Blank the display so the ILIM-capped input isn't starved -> reach true CV term."""
    print("\n=== CHARGE ===")
    prompt("PLUG IN USB.")
    set_screen(mqtt, not blank_display)
    set_charge(mqtt, "auto")
    return rec.collect(minutes * 60, "charge")


def emit_profile_stub(offsets, out_path):
    """STUB — emits a candidate ha_batt_profile_t JSON. The offset deltas ARE computed rigorously;
    the LUT is left as the linear-provisional default pending the fit-method decision (see the
    OPEN METHOD QUESTIONS in the module docstring). Do NOT treat the LUT here as characterized."""
    def avg(rows, key):
        vals = [r.get(key) for r in rows if isinstance(r.get(key), (int, float))]
        return sum(vals) / len(vals) if vals else None
    prof = {
        "version": "candidate", "date": time.strftime("%Y-%m-%d"), "method": "auto-harness",
        "offsets_measured": {},
        "lut": "UNSET — linear-provisional default retained until the fit method is settled",
    }
    if offsets:
        base = avg(offsets["base"], "batt_mv")
        for name, key in (("display_off", "display_off"), ("usb_on", "usb_on"), ("charging", "charging")):
            v = avg(offsets[key], "batt_mv")
            prof["offsets_measured"][name + "_delta_mv"] = (round(v - base) if (v and base) else None)
    with open(out_path, "w") as f:
        json.dump(prof, f, indent=2)
    print(f"\ncandidate profile (offsets only) -> {out_path}")
    print(json.dumps(prof, indent=2))


def main():
    ap = argparse.ArgumentParser(description="D1001 battery characterization harness (ADR-0024)")
    ap.add_argument("--broker", default=BROKER)
    ap.add_argument("--out", default="battchar.csv", help="telemetry CSV output")
    ap.add_argument("--profile-out", default="battchar_profile.json")
    ap.add_argument("--settle", type=float, default=90, help="seconds per offset step (>= ~4 samples @15s)")
    ap.add_argument("--discharge-min", type=float, default=90)
    ap.add_argument("--charge-min", type=float, default=180)
    ap.add_argument("--floor-mv", type=int, default=3520, help="advisory safe-stop watch value")
    ap.add_argument("--phases", default="offsets,discharge,charge",
                    help="comma list: offsets,discharge,charge")
    ap.add_argument("--blank-charge", action="store_true", help="hold display OFF during charge (reach CV term)")
    ap.add_argument("--dry-run", action="store_true", help="print commands, don't touch MQTT")
    args = ap.parse_args()

    mqtt = Mqtt(broker=args.broker, dry_run=args.dry_run)
    rec = Recorder(mqtt, args.out)
    rec.open()
    phases = [p.strip() for p in args.phases.split(",") if p.strip()]
    offsets = None
    try:
        if "offsets" in phases:
            offsets = measure_offsets(rec, mqtt, args.settle)
        if "discharge" in phases:
            discharge(rec, mqtt, args.discharge_min, args.floor_mv)
        if "charge" in phases:
            charge_to_top(rec, mqtt, args.charge_min, args.blank_charge)
    finally:
        set_charge(mqtt, "auto")   # ALWAYS leave the charger in auto (safe default)
        emit_profile_stub(offsets, args.profile_out)
        print(f"\ntelemetry -> {args.out}   (charge left in AUTO)")


if __name__ == "__main__":
    main()
