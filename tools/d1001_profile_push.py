#!/usr/bin/env python3
"""Deploy a D1001 battery profile as DATA over MQTT — no reflash (ADR-0024 §5).

Builds the profile JSON the firmware's cmd/profile handler (ha_battery_profile_rt) accepts, and pushes it
to d1001-beachhead/cmd/profile. The panel validates it, hot-swaps it into the live gauge, and persists it
to NVS (survives reboot). The active curve is echoed retained on d1001-beachhead/profile with a `source`
tag (default | pushed | nvs).

  # show the active profile the panel is running
  tools/d1001_profile_push.py --get

  # push the v4 curve (defaults below mirror ha_batt_profile_d1001_default); --lut-csv loads a fresh fit
  tools/d1001_profile_push.py --push
  tools/d1001_profile_push.py --push --lut-csv /path/to/d1001_v4_lut.csv --version v5 --date 2026-07-10

  # revert the panel to its baked-in default (clears the stored profile)
  tools/d1001_profile_push.py --default
"""
import argparse
import json
import subprocess
import sys
import time

REQ_TOPIC = "d1001-beachhead/cmd/profile"
RES_TOPIC = "d1001-beachhead/profile"

# v4 default — mirrors firmware/components/ha_battery_profile/ha_battery_profile.c (the baked fallback).
V4 = {
    "version": "v4", "date": "2026-07-04", "method": "auto-discharge",
    "off_display_off_mv": 40, "off_usb_mv": 128, "off_charging_mv": 80,
    "run_floor_mv": 3450, "warn_mv": 3520, "warn_clear_mv": 3580,
    "boot_gate_mv": 3550, "boot_release_mv": 3650,
    "lut": [3450, 3466, 3482, 3498, 3508, 3526, 3540, 3550, 3564, 3578, 3594,
            3611, 3624, 3636, 3644, 3660, 3672, 3688, 3712, 3742, 3780],
}


def lut_from_csv(path):
    """Read a 21-point LUT from tools/e1001_profile.py's `d1001 --csv` output (soc_pct,v_mv)."""
    import csv
    rows = sorted(((int(r["soc_pct"]), int(r["v_mv"])) for r in csv.DictReader(open(path))))
    return [v for _, v in rows]


def build(args):
    p = dict(V4)
    if args.lut_csv:
        p["lut"] = lut_from_csv(args.lut_csv)
    if args.version:
        p["version"] = args.version
    if args.date:
        p["date"] = args.date
    if args.method:
        p["method"] = args.method
    return p


def pub(broker, payload):
    subprocess.run(["mosquitto_pub", "-h", broker, "-t", REQ_TOPIC, "-m", payload], check=True)


def pub_confirm(broker, payload):
    """Publish, and CATCH the panel's async response: subscribe to ack + profile BEFORE publishing (the
    apply is async on-device — parse+NVS+republish), then print what the panel actually says. This is the
    definitive applied/rejected signal; a bare --get right after a --push races the retained update."""
    sub = subprocess.Popen(
        ["mosquitto_sub", "-h", broker, "-v", "-t", "d1001-beachhead/ack", "-t", RES_TOPIC],
        stdout=subprocess.PIPE, text=True)
    time.sleep(0.4)                      # let the subscriber connect before we publish
    pub(broker, payload)
    time.sleep(2.5)                      # let the panel validate + hot-swap + persist + republish
    sub.terminate()
    lines = (sub.stdout.read() or "").strip().splitlines()
    applied = rejected = None
    for ln in lines:
        topic, _, body = ln.partition(" ")
        if topic.endswith("/ack") and '"profile"' in body:
            if "applied" in body:  applied = body
            if "rejected" in body: rejected = body
    if rejected:
        print(f"REJECTED by panel: {rejected}", file=sys.stderr)
    elif applied:
        print(f"APPLIED: {applied}")
    else:
        print("no applied/rejected ack seen in 2.5s — is the NEW firmware flashed and the panel online?\n"
              "  (raw ack/profile traffic below)\n  " + "\n  ".join(lines), file=sys.stderr)


def get(broker):
    """Read the retained active-profile status once."""
    p = subprocess.run(["mosquitto_sub", "-h", broker, "-t", RES_TOPIC, "-W", "3", "-C", "1"],
                       capture_output=True, text=True)
    out = p.stdout.strip()
    if not out:
        print("no retained profile status — is the panel online? (send --get again after it boots)",
              file=sys.stderr)
        return
    try:
        d = json.loads(out)
        print(f"active: {d.get('version')} ({d.get('date')} / {d.get('method')})  source={d.get('source')}")
        print(f"  floors: run={d.get('run_floor_mv')} warn={d.get('warn_mv')}/{d.get('warn_clear_mv')} "
              f"boot={d.get('boot_gate_mv')}/{d.get('boot_release_mv')}")
        print(f"  lut(100%%={d.get('lut', [0])[-1]} 0%%={d.get('lut', [0])[0]}): {d.get('lut')}")
    except ValueError:
        print(out)


def main():
    ap = argparse.ArgumentParser(description="Deploy a D1001 battery profile over MQTT (ADR-0024 §5)")
    ap.add_argument("--broker", default="192.168.0.210")
    ap.add_argument("--push", action="store_true", help="push the profile (built from V4 + overrides)")
    ap.add_argument("--get", action="store_true", help="print the active profile the panel reports")
    ap.add_argument("--default", action="store_true", help="revert the panel to its baked-in default")
    ap.add_argument("--lut-csv", help="load the 21-pt LUT from an e1001_profile.py d1001 --csv file")
    ap.add_argument("--version"); ap.add_argument("--date"); ap.add_argument("--method")
    ap.add_argument("--dry-run", action="store_true", help="print the JSON, don't publish")
    a = ap.parse_args()

    if a.default:
        if a.dry_run: print("default"); return
        pub(a.broker, "default")
        print("sent 'default' — panel reverts to the baked-in curve; check with --get")
    elif a.push:
        payload = json.dumps(build(a), separators=(",", ":"))
        if a.dry_run:
            print(payload); return
        print(f"pushing {json.loads(payload)['version']} ({len(payload)} bytes) …")
        pub_confirm(a.broker, payload)
        print("--- panel now reports ---")
        get(a.broker)   # retained is settled by now (pub_confirm already waited)
    elif a.get:
        get(a.broker)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
