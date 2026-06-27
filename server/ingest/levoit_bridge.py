#!/usr/bin/env python3
"""
ESPHome (Levoit purifier) → canonical-topic bridge.

A Levoit Vital 200S reflashed to local ESPHome (see provisioning/levoit/) publishes one MQTT topic PER
ENTITY — e.g. `levoit-office/sensor/pm_2_5/state`, `levoit-office/fan/fan/speed_level/state` — rather than
one JSON blob like Tasmota. This bridge is the ESPHome sibling of `server/ingest/tasmota_bridge.py`: a thin,
stateless-per-message translator that maps each device's per-entity state into the SAME canonical message the
writer already ingests —

    home/<area>/<device_id>/state   (qos 1, retain)

so the purifier needs no writer or dashboard change (same UNIQUE(device_id,ts,metric) idempotency).

Because ESPHome emits entities independently (and republishes unchanged values on its own cadence), the bridge
keeps the latest value of every mapped metric per device and emits a FULL snapshot whenever any value actually
changes — giving a complete retained canonical state without re-writing unchanged metrics every interval.

Config (instance/levoit-devices.yaml) maps an ESPHome node name (the MQTT topic prefix) -> our identity:

    levoit-office:                 # ESPHome `name:` == topic prefix
      device_id: levoit_office
      area: office
      device_type: air_purifier

Metrics emitted: pm25_ugm3, aqi, cadr, filter_life_pct, fan_on (1/0), fan_speed (1-4), filter_low (1/0).
Control (fan on/off + speed + mode) is the reverse path — publish to `<name>/.../command`; that lives in the
control layer, not here (read-only bridge). Transport is stamped `wifi-mqtt`; ts is the bridge's UTC receive
time (an air-gapped ESPHome clock may be unsynced), matching tasmota_bridge / edge_mapper stamp-on-ingest.
"""
import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import paho.mqtt.client as mqtt                       # noqa: E402
from server.util.mqtt_creds import apply_credentials  # noqa: E402

try:
    import yaml
except ImportError:                                   # pragma: no cover
    yaml = None

BROKER_HOST = os.environ.get("HA_BROKER_HOST", "192.168.0.200")
BROKER_PORT = int(os.environ.get("HA_BROKER_PORT", "1883"))
MESSAGE_SCHEMA = 1
log = logging.getLogger("ha.levoit")


def _onoff(v: str):
    s = str(v).strip().upper()
    if s in ("ON", "TRUE", "1"):
        return 1.0
    if s in ("OFF", "FALSE", "0"):
        return 0.0
    return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ESPHome topic suffix (after `<name>/`) -> (canonical metric, converter). Suffixes are the live layout proven
# on levoit-office 2026-06-27 (provisioning/levoit/README.md). Unmapped entities (select/number/switch/*,
# version/error sensors) are metadata/advanced controls, not time-series metrics — ignored here.
_METRIC_MAP: dict[str, tuple[str, callable]] = {
    "sensor/pm_2_5/state":              ("pm25_ugm3", _num),
    "sensor/aqi/state":                 ("aqi", _num),
    "sensor/current_cadr/state":        ("cadr", _num),
    "sensor/filter__/state":            ("filter_life_pct", _num),
    "fan/fan/state":                    ("fan_on", _onoff),
    "fan/fan/speed_level/state":        ("fan_speed", _num),
    "binary_sensor/filter_low/state":   ("filter_low", _onoff),
}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_registry(path: Path) -> dict[str, dict]:
    if not path.exists():
        log.warning("Levoit registry not found at %s — no devices will be bridged", path)
        return {}
    if yaml is None:
        log.error("pyyaml not installed — cannot read %s", path)
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    out = {}
    for name, cfg in data.items():
        cfg = cfg or {}
        out[str(name)] = {
            "device_id": cfg.get("device_id", str(name).replace("-", "_")),
            "area": cfg.get("area", "unknown"),
            "device_type": cfg.get("device_type", "air_purifier"),
        }
    return out


class LevoitBridge:
    def __init__(self, registry: dict[str, dict], client: mqtt.Client):
        self._registry = registry                      # esphome name -> identity
        self._mqtt = client
        self._state: dict[str, dict[str, float]] = {}   # device_id -> {metric: value} (accumulated)
        self._online: dict[str, bool] = {}
        self._unknown: set[str] = set()

    def on_connect(self, client, userdata, flags, rc, properties=None):
        if rc != 0:
            log.error("MQTT connect failed rc=%s", rc)
            return
        for name in self._registry:
            client.subscribe(f"{name}/#", qos=0)
        log.info("connected; bridging %d Levoit/ESPHome device(s): %s",
                 len(self._registry), ", ".join(self._registry) or "(none)")

    def on_message(self, client, userdata, msg: mqtt.MQTTMessage):
        name, _, suffix = msg.topic.partition("/")
        reg = self._registry.get(name)
        if not reg:
            if name not in self._unknown:
                self._unknown.add(name)
                log.warning("telemetry from UNKNOWN ESPHome node %r — add it to the registry", name)
            return
        try:
            raw = msg.payload.decode().strip()
        except UnicodeDecodeError:
            return
        device_id = reg["device_id"]

        if suffix == "status":                          # ESPHome availability LWT (online/offline)
            self._online[device_id] = (raw.lower() == "online")
            return

        mapped = _METRIC_MAP.get(suffix)
        if not mapped:
            return                                      # metadata / advanced control entity — not a metric
        metric, conv = mapped
        value = conv(raw)
        if value is None:
            return

        cur = self._state.setdefault(device_id, {})
        if cur.get(metric) == value:
            return                                      # unchanged — skip (avoid re-writing on ESPHome republish)
        cur[metric] = value
        self._emit(reg, device_id, cur)

    def _emit(self, reg: dict, device_id: str, metrics: dict[str, float]):
        out_topic = f"home/{reg['area']}/{device_id}/state"
        out = {
            "schema": MESSAGE_SCHEMA,
            "device_id": device_id,
            "device_type": reg["device_type"],
            "area": reg["area"],
            "ts": _utc_now(),
            "transport": "wifi-mqtt",
            "metrics": dict(metrics),                   # full current snapshot (deduped on change)
            "meta": {"esphome": True, "online": self._online.get(device_id, True)},
        }
        self._mqtt.publish(out_topic, json.dumps(out), qos=1, retain=True)
        log.debug("emit %s %s", out_topic, metrics)


def main() -> None:
    p = argparse.ArgumentParser(description="ESPHome (Levoit) → canonical-topic bridge")
    p.add_argument("--registry", default="instance/levoit-devices.yaml", type=Path)
    p.add_argument("--broker", default=BROKER_HOST)
    p.add_argument("--broker-port", default=BROKER_PORT, type=int)
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s", stream=sys.stdout)

    registry = load_registry(args.registry)
    log.info("Levoit registry: %d device(s)", len(registry))

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    apply_credentials(client)
    bridge = LevoitBridge(registry, client)
    client.on_connect = bridge.on_connect
    client.on_message = bridge.on_message
    client.connect(args.broker, args.broker_port, keepalive=60)
    client.loop_forever()


if __name__ == "__main__":
    main()
