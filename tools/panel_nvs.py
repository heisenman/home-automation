#!/usr/bin/env python3
"""Mint a D1001 panel's per-unit identity blob (NVS "ha" namespace, @0x9000) bound to the attached chip.

Every D1001 runs the same image (OTA identity "d1001-beachhead@…"); what makes a unit distinct is its
node_id — MQTT topics, broker client id, edge node id. The FIRST panel has no NVS node_id and runs as
"d1001-beachhead". Any further unit needs its own, or the two share a broker client id and evict each other.

Reuses server.maintenance.edge_flash (detect + build_nvs_blob): the MAC is read off the silicon and baked
in as bind_mac, so the blob can't give another chip this identity (ADR-0020 gate). No wifi in the blob —
the panel's compiled secrets.h defaults (air-gap SSID) apply. No cmd_secret (ADR-0036: node-born).

    venv/bin/python tools/panel_nvs.py --node-id d1001_2 --out /tmp/x/nvs.bin
    then: esptool.py --port <port> write_flash 0x9000 /tmp/x/nvs.bin   (panel.sh flash-unit does both)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.maintenance import edge_flash as ef  # noqa: E402

PANEL_IMAGE = "d1001-beachhead"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--node-id", required=True, help="this unit's name, slug [a-z0-9_] (e.g. d1001_2)")
    ap.add_argument("--port", default=None, help="serial port (default: the only Espressif USB-JTAG)")
    ap.add_argument("--broker", default="mqtt://192.168.1.200:1883", help="broker URI (ha-2 VIP, air-gap)")
    ap.add_argument("--ntp", default="192.168.1.210")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()

    if a.node_id == PANEL_IMAGE:
        sys.exit("refusing: that is the first panel's name — a 2nd unit needs its own")
    port = a.port
    if not port:
        ports = sorted(Path("/dev/serial/by-id").glob("usb-Espressif_USB_JTAG*-if00"))
        if len(ports) != 1:
            sys.exit(f"found {len(ports)} Espressif USB-JTAG ports — pass --port")
        port = str(ports[0])
    chip = ef.detect(port)
    if "P4" not in (chip.get("chip") or "").upper() and chip.get("target_raw") != "esp32p4":
        sys.exit(f"attached chip is {chip.get('chip')!r}, not an ESP32-P4 panel")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    ef.build_nvs_blob({"node_id": a.node_id, "mac": chip["mac"], "broker_uri": a.broker,
                       "ntp_server": a.ntp, "ota_host": "", "gas_sensor": "none"}, a.out)
    print(f"minted {a.out} — node_id={a.node_id} broker={a.broker}, bound to the attached chip")  # no MAC in logs
    return 0


if __name__ == "__main__":
    sys.exit(main())
