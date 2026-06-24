"""
Edge mapper — resolve edge-relayed BLE readings to canonical device topics.

Edge nodes (ESP32-C6) are deliberately dumb (ADR-0001: the dictator owns the registry). A node
decodes a SwitchBot advertisement and publishes it keyed by MAC on:

    home/edge/<node>/<mac>/adv

This service subscribes to those, looks up MAC → device_id/area in the authoritative registry, and
republishes the canonical message the writer already ingests:

    home/<area>/<device_id>/state      (qos 1, retain)

So adding edge nodes needs no writer or dashboard change. The mapper is a thin, stateless
translator: multiple nodes seeing the same meter is fine — the writer's UNIQUE(device_id, ts, metric)
dedups, and meta.node / meta.rssi record which node relayed each reading.

Edge adv payload (published by the C6):
  {
    "schema": 1, "node": "c6-bench", "mac": "AA:BB:CC:00:00:01",
    "device_type": "switchbot_meter_outdoor", "ts": "2026-06-20T01:23:45Z",
    "transport": "ble-adv", "metrics": {"temperature_c": 22.7, "humidity_pct": 39, "battery_pct": 100},
    "meta": {"rssi": -78}
  }
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root (run as a script)
from server.util.mqtt_creds import apply_credentials  # noqa: E402
from server.mesh.assign import Assigner  # noqa: E402

import paho.mqtt.client as mqtt
import yaml

log = logging.getLogger("ha.edge_mapper")

BROKER_HOST: str = os.environ.get("HA_BROKER", "localhost")
BROKER_PORT: int = int(os.environ.get("HA_BROKER_PORT", "1883"))
SUBSCRIBE_TOPIC: str = "home/edge/+/+/adv"
MESSAGE_SCHEMA: int = 1


def load_registry(path: Path) -> dict[str, dict]:
    """MAC → device-info dict, MACs normalised to uppercase (same as the scanner)."""
    if not path.exists():
        log.warning("Registry not found at %s — all edge readings will be dropped as unknown", path)
        return {}
    with path.open() as f:
        raw = yaml.safe_load(f) or {}
    return {mac.upper(): info for mac, info in raw.get("devices", {}).items()}


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EdgeMapper:
    def __init__(self, registry: dict[str, dict], client: mqtt.Client, relay_dedup: bool = False):
        self._registry = registry
        self._mqtt = client
        self._unknown_seen: set[str] = set()
        # ADR-0015 Phase A: when on, republish a meter only from its single preferred source (drops the
        # rest); local readings flow in as node "local" via home/edge/local/<mac>/adv. Default OFF =
        # today's behaviour (every source republishes; the writer's UNIQUE(...) dedups at the DB).
        self._dedup = relay_dedup
        self._assigner = Assigner() if relay_dedup else None

    def on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            client.subscribe(SUBSCRIBE_TOPIC, qos=1)
            log.info("connected; subscribed to %s", SUBSCRIBE_TOPIC)
        else:
            log.error("MQTT connect failed rc=%s", rc)

    def on_message(self, client, userdata, msg: mqtt.MQTTMessage):
        try:
            payload = json.loads(msg.payload.decode())
        except (ValueError, UnicodeDecodeError) as exc:
            log.warning("bad edge payload on %s: %s", msg.topic, exc)
            return

        mac = str(payload.get("mac", "")).upper()
        if not mac:
            log.warning("edge payload missing mac on %s", msg.topic)
            return

        reg = self._registry.get(mac)
        if not reg:
            if mac not in self._unknown_seen:
                self._unknown_seen.add(mac)
                log.warning("edge reading from UNKNOWN mac=%s (node=%s) — add it to the registry",
                            mac, payload.get("node"))
            return

        device_id = reg["device_id"]
        area = reg.get("area", "unknown")
        device_type = reg.get("device_type") or payload.get("device_type", "unknown")
        metrics = payload.get("metrics", {})
        if not metrics:
            return

        # node id: explicit field, else 3rd topic segment home/edge/<node>/<mac>/adv
        parts = msg.topic.split("/")
        node = payload.get("node") or (parts[2] if len(parts) > 3 else "unknown")
        in_meta = payload.get("meta", {}) or {}

        # ADR-0015 Phase A preferred-source gate: observe this source's reach, and if a different source
        # is the assigned one for this device, DROP (it'll be republished from its preferred source).
        if self._dedup:
            nowt = time.time()
            self._assigner.observe(device_id, node, in_meta.get("rssi"), nowt)
            pref = self._assigner.preferred(device_id, nowt)
            if pref is not None and node != pref:
                log.debug("dedup drop %s from %s (preferred=%s)", device_id, node, pref)
                return

        out_topic = f"home/{area}/{device_id}/state"
        out = {
            "schema": payload.get("schema", MESSAGE_SCHEMA),
            "device_id": device_id,
            "device_type": device_type,
            "area": area,
            "ts": payload.get("ts") or _utc_now(),
            "transport": payload.get("transport", "ble-adv"),
            "metrics": metrics,
            "meta": {"rssi": in_meta.get("rssi"), "mac": mac, "node": node},
        }
        self._mqtt.publish(out_topic, json.dumps(out), qos=1, retain=True)
        log.debug("mapped %s (%s) -> %s %s", mac, node, out_topic, metrics)


def main() -> None:
    p = argparse.ArgumentParser(description="Edge → canonical-topic mapper")
    p.add_argument("--registry", default="instance/devices.yaml", type=Path)
    p.add_argument("--broker", default=BROKER_HOST)
    p.add_argument("--broker-port", default=BROKER_PORT, type=int)
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--relay-dedup", action="store_true",
                   default=os.environ.get("HA_RELAY_DEDUP", "").lower() in ("1", "true", "yes"),
                   help="ADR-0015 Phase A: republish each meter only from its preferred source (default OFF)")
    args = p.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s", stream=sys.stdout)

    registry = load_registry(args.registry)
    log.info("registry loaded: %d known devices (relay_dedup=%s)", len(registry), args.relay_dedup)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    apply_credentials(client)
    mapper = EdgeMapper(registry, client, relay_dedup=args.relay_dedup)
    client.on_connect = mapper.on_connect
    client.on_message = mapper.on_message

    attempt = 0
    while True:
        try:
            client.connect(args.broker, args.broker_port, keepalive=60)
            break
        except Exception as exc:
            attempt += 1
            wait = min(2 ** attempt, 60)
            log.warning("connect attempt %d failed: %s — retry in %ds", attempt, exc, wait)
            time.sleep(wait)

    client.loop_forever()


if __name__ == "__main__":
    main()
