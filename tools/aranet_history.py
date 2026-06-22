#!/usr/bin/env python3
"""
Aranet history backfill — pull the device's internal log over GATT and ingest it idempotently.

Unlike SwitchBot (reverse-engineered), the `aranet4` library implements the history protocol, so this
is a thin wrapper: connect to the Aranet, download all stored RecordItems (temp/humidity/pressure/
radon at the device's logging interval), and publish each on the canonical state topic the writer
already ingests — with the record's REAL timestamp. The writer dedups on (device_id, ts, metric), so
re-running is safe (a second pull inserts only genuinely new records).

Needs a Python BLE central IN RANGE of the device (e.g. .112 with the unit on the desk). transport is
labelled `aranet-history` to distinguish from the live `ble-adv` relay.

  python3 tools/aranet_history.py --broker 192.168.0.245 --registry instance/devices.yaml
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
from aranet4 import client as aranet_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.ingest.edge_mapper import load_registry

_U16_NO_READING = 0xFFFF
_LOCAL_TZ = datetime.now().astimezone().tzinfo   # records come back in system-local naive time


def _utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_LOCAL_TZ)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _metrics(item) -> dict:
    m: dict = {}
    if item.temperature is not None and item.temperature > -100:
        m["temperature_c"] = round(float(item.temperature), 2)
    if item.humidity is not None and item.humidity >= 0:
        m["humidity_pct"] = round(float(item.humidity), 1)
    if item.pressure is not None and item.pressure > 0:
        m["pressure_hpa"] = round(float(item.pressure), 1)
    radon = getattr(item, "radon_concentration", None)
    if radon is not None and 0 <= radon < _U16_NO_READING:
        m["radon_bqm3"] = int(radon)
    return m


def main() -> None:
    p = argparse.ArgumentParser(description="Backfill Aranet history via the aranet4 library")
    p.add_argument("--mac", help="override; else taken from the registry (aranet-type device)")
    p.add_argument("--registry", default="instance/devices.yaml", type=Path)
    p.add_argument("--broker", default="localhost")
    p.add_argument("--broker-port", type=int, default=1883)
    p.add_argument("--dry-run", action="store_true", help="pull + summarize, do not publish")
    a = p.parse_args()

    reg = load_registry(a.registry)
    if a.mac:
        mac = a.mac.upper(); info = reg.get(mac, {})
    else:
        hit = next(((m, i) for m, i in reg.items()
                    if str(i.get("device_type", "")).startswith("aranet")), None)
        if not hit:
            sys.exit("no aranet device in registry; pass --mac")
        mac, info = hit
    device_id = info.get("device_id", mac.replace(":", "").lower())
    area = info.get("area", "unknown")
    device_type = info.get("device_type", "aranet_radon_plus")

    print(f"pulling history from {mac} ({device_id}) — connecting over GATT (in range required)...")
    rec = aranet_client.get_all_records(mac, {}, remove_empty=True)
    items = rec.value
    print(f"  device reports {rec.records_on_device} records; pulled {len(items)}")
    if not items:
        return
    print(f"  range: {_utc(items[0].date)} .. {_utc(items[-1].date)}")

    if a.dry_run:
        s = items[len(items) // 2]
        print(f"  [dry-run] sample mid record: {_utc(s.date)} {_metrics(s)}")
        return

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.connect(a.broker, a.broker_port, 30); c.loop_start()
    n = 0
    for i, item in enumerate(items):
        m = _metrics(item)
        if not m:
            continue
        msg = {"schema": 1, "device_id": device_id, "device_type": device_type, "area": area,
               "ts": _utc(item.date), "transport": "aranet-history", "metrics": m,
               "meta": {"mac": mac, "source": "aranet4-lib"}}
        c.publish(f"home/{area}/{device_id}/state", json.dumps(msg), qos=1, retain=False)
        n += 1
        if i % 200 == 0:
            time.sleep(0.2)        # gentle pacing so the writer/broker keep up
    info_pub = c.publish(f"home/{area}/{device_id}/state", json.dumps(msg), qos=1, retain=False)
    info_pub.wait_for_publish(timeout=10)
    time.sleep(1.0)
    c.loop_stop(); c.disconnect()
    print(f"  published {n} history records (transport=aranet-history); writer dedups by (device_id,ts,metric)")


if __name__ == "__main__":
    main()
