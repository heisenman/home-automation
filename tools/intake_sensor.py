#!/usr/bin/env python3
"""Programmatic BLE sensor intake — discover an unregistered SwitchBot from the live
scanner's discovery stream, then register it into the device registry.

This is the code-first version of what will become the PWA "Add a sensor" flow. It reuses
exactly what the running system already does — no separate BLE stack, no pairing, no cloud:

  ha-scanner (already running, passive BLE observer) decodes every SwitchBot/Aranet advert.
  A REGISTERED mac → canonical home/<area>/<id>/state. An UNKNOWN mac (new device, or a
  rotating BLE address) → home/unknown/<mac>/raw instead, so it's inspectable during onboarding.

  watch     Subscribe to home/unknown/+/raw, decode the raw adverts live, and rank candidates
            by signal strength. The just-powered-on meter you're holding next to this box is the
            strongest RSSI, appears the instant you flip it on, and (for the outdoor unit) decodes
            as model 'switchbot_meter_outdoor'. Prints a ready-to-paste `register` command.

  register  Validate + append {mac, device_id, device_type, area, capabilities} to
            instance/devices.yaml (via server.device_registry — atomic, .bak, header-preserving),
            then reload ha-scanner/ha-edge-mapper so it starts publishing canonical state.

SwitchBot meters broadcast temperature/humidity/battery over BLE out of the box (batteries in) —
the SwitchBot app is NOT required to intake one; it only listens.

  # Watch for 90s — run this the moment you power the new meter on, held next to this box:
  python3 tools/intake_sensor.py watch --seconds 90
  # Register the candidate it surfaced (device_type auto-detected; --reload restarts the scanner):
  python3 tools/intake_sensor.py register --mac D1:2D:AA:BB:CC:DD --device-id meter_patio --area patio --reload
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import paho.mqtt.client as mqtt  # noqa: E402

from server.ingest.discovery import decode_raw as _decode_raw  # noqa: E402  (shared with GET /api/v1/discover)
from server.util.mqtt_creds import apply_credentials  # noqa: E402
from server import device_registry  # noqa: E402

DEFAULT_REGISTRY = REPO / "instance" / "devices.yaml"
DEFAULT_MQTT_ENV = REPO / "instance" / "mqtt.env"
DISCOVERY_TOPIC = "home/unknown/+/raw"
DEFAULT_CAPS = ["temperature", "humidity", "battery"]


def _load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines from an env file into os.environ (so apply_credentials sees the broker
    creds the ha-scanner service gets via systemd EnvironmentFile). Existing env wins."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def cmd_watch(args) -> int:
    _load_env_file(Path(args.mqtt_env))
    known = device_registry.load_devices(Path(args.registry))  # {MAC: {...}}
    seen: dict[str, dict] = {}  # mac -> candidate row

    def on_connect(client, userdata, flags, rc, properties=None):
        if rc == 0:
            client.subscribe(DISCOVERY_TOPIC, qos=0)
            print(f"[watch] subscribed to {DISCOVERY_TOPIC} on {args.broker}:{args.port} — "
                  f"power on the new meter now (hold it next to this box).", flush=True)
        else:
            print(f"[watch] MQTT connect failed rc={rc}", file=sys.stderr, flush=True)

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except (ValueError, UnicodeDecodeError):
            return
        mac = str(payload.get("mac", "")).upper()
        if not mac:
            return
        rssi = int(payload.get("rssi", 0))
        dec = _decode_raw(payload)
        row = seen.get(mac)
        first = row is None
        if first:
            row = {"mac": mac, "brand": payload.get("brand", "?"), "count": 0,
                   "rssi": rssi, "rssi_max": rssi, "registered": mac in known}
            seen[mac] = row
        row["count"] += 1
        row["rssi"] = rssi
        row["rssi_max"] = max(row["rssi_max"], rssi)
        if dec:
            row.update(dec)
        if first:
            _print_sighting(row, prefix="NEW  ")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    apply_credentials(client)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.broker, args.port, keepalive=60)
    client.loop_start()

    deadline = time.monotonic() + args.seconds
    try:
        while time.monotonic() < deadline:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    client.loop_stop()
    client.disconnect()

    _print_summary(seen, known)
    return 0


def _print_sighting(row: dict, prefix: str = "") -> None:
    t = row.get("temperature_c")
    tf = f"{_c_to_f(t):.1f}°F" if t is not None else "  ?  "
    h = row.get("humidity_pct")
    hs = f"{h:>3}%RH" if h is not None else "  ? "
    b = row.get("battery_pct")
    bs = f"batt {b:>3}%" if b is not None else "batt   ?"
    dt = row.get("device_type", row.get("brand", "?"))
    flag = "  << OUTDOOR" if dt == "switchbot_meter_outdoor" else ""
    reg = "  [ALREADY REGISTERED]" if row.get("registered") else ""
    print(f"{prefix}{row['mac']}  rssi {row['rssi']:>4}dBm  {dt:<26} {tf:>7} {hs} {bs}{flag}{reg}",
          flush=True)


def _print_summary(seen: dict, known: dict) -> None:
    print("\n" + "=" * 88)
    if not seen:
        print("No unregistered SwitchBot/Aranet adverts heard.\n"
              "  • Is the meter powered (batteries in) and within a few metres of this box?\n"
              "  • ha-scanner debounces the raw topic to 1 msg/60s per device — try a longer --seconds.\n"
              "  • Confirm ha-scanner is running:  systemctl is-active ha-scanner", flush=True)
        return
    rows = sorted(seen.values(), key=lambda r: r["rssi_max"], reverse=True)
    print(f"CANDIDATES heard ({len(rows)}) — strongest signal first (closest ≈ the one you're holding):\n")
    for r in rows:
        _print_sighting(r, prefix="  ")
    # Auto-suggest a register command for the best decodable, unregistered candidate.
    best = next((r for r in rows if r.get("device_type", "").startswith("switchbot")
                 and not r.get("registered")), None)
    print("=" * 88)
    if best:
        dt = best["device_type"]
        print("\nTo register the top candidate (edit device_id/area to your room):\n"
              f"  python3 tools/intake_sensor.py register \\\n"
              f"      --mac {best['mac']} --device-type {dt} \\\n"
              f"      --device-id meter_<room> --area <room> --reload", flush=True)
        if dt == "switchbot_meter_outdoor":
            print("\nNOTE: the outdoor meter uses a ROTATING BLE address. Register the MAC it shows NOW;\n"
                  "      if it later rotates, the binding drifts and you re-register the new MAC.\n"
                  "      Confirm identity before committing: breathe on the sensor and watch humidity jump\n"
                  "      on THIS mac (re-run watch; readings update ~once/60s).", flush=True)
    else:
        print("\nNo unregistered SwitchBot candidate to auto-suggest "
              "(all heard were already-registered or non-SwitchBot).", flush=True)


def _reload_scanner() -> None:
    """Restart ha-scanner + ha-edge-mapper so the new registry entry is loaded."""
    units = ["ha-scanner", "ha-edge-mapper"]
    # ha-* restart is in the NOPASSWD scope; fall back to -S with instance/.rootpwd if narrowed.
    try:
        subprocess.run(["sudo", "-n", "systemctl", "restart", *units], check=True)
        print(f"[reload] restarted {' '.join(units)}", flush=True)
        return
    except subprocess.CalledProcessError:
        pass
    rootpwd = REPO / "instance" / ".rootpwd"
    if rootpwd.exists():
        pw = rootpwd.read_text()
        subprocess.run(["sudo", "-S", "systemctl", "restart", *units],
                       input=pw, text=True, check=True)
        print(f"[reload] restarted {' '.join(units)} (via .rootpwd)", flush=True)
    else:
        print(f"[reload] could not restart automatically — run:  "
              f"sudo systemctl restart {' '.join(units)}", file=sys.stderr, flush=True)


def cmd_register(args) -> int:
    path = Path(args.registry)
    existing = device_registry.load_devices(path)
    body = {
        "mac": args.mac,
        "device_id": args.device_id,
        "device_type": args.device_type,
        "area": args.area,
        "capabilities": [c.strip() for c in args.capabilities.split(",") if c.strip()],
    }
    if args.notes:
        body["notes"] = args.notes
    entry, err = device_registry.validate_new_device(body, existing)
    if err:
        print(f"[register] rejected: {err}", file=sys.stderr, flush=True)
        return 2
    device_registry.append_device(path, entry)
    print(f"[register] added {entry['mac']} → device_id={entry['device_id']} "
          f"type={entry['device_type']} area={entry['area']} (in {path})", flush=True)
    if args.reload:
        _reload_scanner()
        print(f"[register] done — expect canonical state on  home/{entry['area']}/{entry['device_id']}/state",
              flush=True)
    else:
        print("[register] NOT reloaded — run with --reload, or: "
              "sudo systemctl restart ha-scanner ha-edge-mapper", flush=True)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Programmatic BLE sensor intake (SwitchBot).")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("watch", help="listen for unregistered SwitchBot adverts and rank candidates")
    w.add_argument("--seconds", type=int, default=90, help="how long to listen (default 90)")
    w.add_argument("--broker", default="127.0.0.1")
    w.add_argument("--port", type=int, default=1883)
    w.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    w.add_argument("--mqtt-env", default=str(DEFAULT_MQTT_ENV))
    w.set_defaults(func=cmd_watch)

    r = sub.add_parser("register", help="append a device to the registry and reload the scanner")
    r.add_argument("--mac", required=True)
    r.add_argument("--device-id", required=True, help="slug [a-z0-9_], e.g. meter_patio")
    r.add_argument("--area", required=True, help="room slug [a-z0-9_], e.g. patio")
    r.add_argument("--device-type", default="switchbot_meter_outdoor",
                   help="default switchbot_meter_outdoor (what the outdoor unit decodes as)")
    r.add_argument("--capabilities", default=",".join(DEFAULT_CAPS))
    r.add_argument("--notes", default="")
    r.add_argument("--reload", action="store_true", help="restart ha-scanner/ha-edge-mapper after adding")
    r.set_defaults(func=cmd_register)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
