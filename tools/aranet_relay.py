#!/usr/bin/env python3
"""
Aranet relay — receive an Aranet's extended-advertising broadcast and publish it on the canonical
state topic the writer already ingests (zero writer/dashboard change, same pattern as edge_mapper).

WHY a relay (not the server scanner): Aranet broadcasts via BLE5 **extended advertising** in
manufacturer data 0x0702. A scanner only sees it if its adapter + stack do ext-adv (BlueZ on a BT5
dongle does; the legacy ESP32-C6 scan path does NOT yet). Run this wherever a capable radio is in
range of the device — today the .112 desktop; eventually an edge node near the crawlspace.

  home/<area>/<device_id>/state   {schema,device_id,device_type,area,ts,transport,metrics,meta}

Publishes once per NEW reading (the device repeats each reading ~1 Hz with a rising 'ago'); the ts is
back-dated by ago_s to when the reading was actually taken.

  python3 tools/aranet_relay.py --broker 192.168.0.245 --registry instance/devices.yaml
"""
import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
from bleak import BleakScanner

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.ingest.decoders import aranet
from server.ingest.edge_mapper import load_registry

NODE = os.uname().nodename


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def run(broker: str, port: int, registry_path: Path, node: str, once: float = 0.0) -> None:
    reg = load_registry(registry_path)
    aranet_macs = {m: info for m, info in reg.items()
                   if str(info.get("device_type", "")).startswith("aranet")}
    if not aranet_macs:
        print("No aranet-type devices in registry — add the Aranet MAC to devices.yaml", file=sys.stderr)
        return
    print(f"relaying {len(aranet_macs)} aranet device(s): {list(aranet_macs)}")

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.connect(broker, port, 30)
    c.loop_start()
    last_key: dict[str, tuple] = {}

    def on_adv(dev, adv):
        mac = dev.address.upper()
        info = aranet_macs.get(mac)
        if not info or aranet.COMPANY_ID not in adv.manufacturer_data:
            return
        out = aranet.decode_manufacturer(mac, adv.manufacturer_data, adv.rssi)
        if not out:
            return
        # de-dup: one publish per new reading (counter/ago resets identify a fresh sample)
        ago = out["meta"]["ago_s"]
        key = (out["metrics"].get("radon_bqm3"), out["metrics"]["temperature_c"], ago < 5)
        # publish when the reading content changes OR a fresh sample (ago small) arrives
        sig = (out["metrics"].get("radon_bqm3"), out["metrics"]["temperature_c"],
               out["metrics"]["pressure_hpa"], out["metrics"]["humidity_pct"])
        if last_key.get(mac) == sig:
            return
        last_key[mac] = sig
        device_id = info.get("device_id", mac.replace(":", "").lower())
        area = info.get("area", "unknown")
        ts = _iso(time.time() - ago)
        msg = {
            "schema": 1, "device_id": device_id, "device_type": out["device_type"],
            "area": area, "ts": ts, "transport": "ble-adv", "metrics": out["metrics"],
            "meta": {"rssi": adv.rssi, "mac": mac, "node": node, **out["meta"]},
        }
        c.publish(f"home/{area}/{device_id}/state", json.dumps(msg), qos=1, retain=True)
        print(f"  {ts} {device_id} {out['metrics']}")

    scanner = BleakScanner(detection_callback=on_adv)
    await scanner.start()
    try:
        if once:
            await asyncio.sleep(once)
        else:
            while True:
                await asyncio.sleep(3600)
    finally:
        await scanner.stop()
        c.loop_stop(); c.disconnect()


def main() -> None:
    p = argparse.ArgumentParser(description="Relay Aranet ext-adv broadcasts to the canonical state topic")
    p.add_argument("--broker", default=os.environ.get("HA_BROKER", "localhost"))
    p.add_argument("--broker-port", type=int, default=int(os.environ.get("HA_BROKER_PORT", "1883")))
    p.add_argument("--registry", default="instance/devices.yaml", type=Path)
    p.add_argument("--node", default=f"aranet-relay@{NODE}")
    p.add_argument("--once", type=float, default=0.0, help="scan this many seconds then exit (test)")
    a = p.parse_args()
    asyncio.run(run(a.broker, a.broker_port, a.registry, a.node, a.once))


if __name__ == "__main__":
    main()
