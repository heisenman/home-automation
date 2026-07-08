#!/usr/bin/env python3
"""repoint_tasmota.py — move a Tasmota device onto (or off) the air-gap net over MQTT (Phase 3/4).

Sends a Backlog to the device via its CURRENT broker to switch its WiFi SSID + MQTT host. The device
then reconnects to the target AP + broker. Fully REVERSIBLE (re-run with household values, or --revert
with explicit --ssid/--psk). Idempotent. See docs/airgap/MIGRATION-DESIGN-LOG.md.

Forward (to air-gap):  repoint_tasmota.py failover_pm            # SSID autohome_airgap, MqttHost .1.210
Revert  (to household): repoint_tasmota.py failover_pm --revert --ssid CTWap_24g --psk <household> --mqtt-host 192.168.0.210
"""
import argparse, subprocess, sys
from pathlib import Path
import yaml

REPO = Path(__file__).resolve().parents[1]
TASMOTA_REG = REPO / "instance/tasmota-devices.yaml"
ENVF = REPO / "instance/openwrt/airgap_router.env"


def load_env(path):
    e = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                e[k.strip()] = v.split("#", 1)[0].strip().strip('"').strip("'")
    return e


def topic_for(device_id):
    data = yaml.safe_load(TASMOTA_REG.read_text()) if TASMOTA_REG.exists() else {}
    data = data or {}
    for topic, entry in data.items():
        if isinstance(entry, dict) and entry.get("device_id") == device_id:
            return topic
    return device_id if device_id in data else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("device_id")
    ap.add_argument("--broker", default="localhost", help="the CURRENT broker to send the command THROUGH (default localhost=.210)")
    ap.add_argument("--ssid", default="autohome_airgap")
    ap.add_argument("--psk", default=None, help="WiFi passphrase (default: WIFI_PSK from instance/openwrt/airgap_router.env)")
    ap.add_argument("--mqtt-host", default="192.168.1.210", help="target MQTT host — a REAL IP, never the VIP")
    ap.add_argument("--revert", action="store_true", help="label a rollback (behaviour identical; give household --ssid/--psk/--mqtt-host)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    topic = topic_for(a.device_id)
    if not topic:
        print(f"✗ no Tasmota topic for device_id '{a.device_id}' in {TASMOTA_REG.name}", file=sys.stderr)
        return 2
    psk = a.psk or load_env(ENVF).get("WIFI_PSK")
    if not psk:
        print("✗ no PSK (pass --psk or set WIFI_PSK in instance/openwrt/airgap_router.env)", file=sys.stderr)
        return 2

    backlog = f"SSId1 {a.ssid}; Password1 {psk}; MqttHost {a.mqtt_host}; MqttPort 1883"
    where = "REVERT to" if a.revert else "repoint to"
    print(f"{where}: {a.device_id} (topic '{topic}') -> SSID={a.ssid} MqttHost={a.mqtt_host}  via broker {a.broker}")
    pub = ["mosquitto_pub", "-h", a.broker, "-t", f"cmnd/{topic}/Backlog", "-m", backlog]
    # redact the psk in the printed command
    print("  cmd:", " ".join(pub[:-1]), repr(backlog.replace(psk, "****")))
    if a.dry_run:
        print("  (dry-run — nothing sent)")
        return 0
    r = subprocess.run(pub, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ✗ publish failed: {r.stderr.strip()}", file=sys.stderr)
        return 1
    print("  ✓ Backlog sent — device will drop off this broker and rejoin the target AP+broker in ~10-20s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
