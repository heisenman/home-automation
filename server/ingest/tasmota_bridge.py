#!/usr/bin/env python3
"""
Tasmota → canonical-topic bridge.

WiFi smart plugs/meters flashed with Tasmota (e.g. SONOFF S31, CSE7766 energy chip) publish their own
JSON on `tele/<topic>/SENSOR` (energy) and `tele/<topic>/STATE` (relay + wifi). Our edge stack is
BLE-advert-centric (home/edge/<node>/<mac>/adv → edge_mapper), so a direct-MQTT WiFi device has no place
to land. This bridge is the WiFi sibling of edge_mapper: a thin, stateless translator that maps each
Tasmota device's telemetry into the SAME canonical message the writer already ingests —

    home/<area>/<device_id>/state   (qos 1, retain)

so adding a Tasmota plug needs no writer or dashboard change (same UNIQUE(device_id,ts,metric) idempotency).

Config (instance/tasmota-devices.yaml) maps a Tasmota %topic% -> our device identity:

    plug_g11:                 # the Tasmota Topic (cmnd/tele/stat prefix segment)
      device_id: plug_g11
      area: infra
      device_type: power_plug

Metrics emitted:
    power_w, apparent_va, power_factor, voltage_v, current_a, energy_kwh, energy_today_kwh   (from ENERGY)
    relay_on (1/0), wifi_rssi_dbm                                                            (from STATE)

Control (turning the plug on/off) is the reverse path — publish `cmnd/<topic>/POWER ON|OFF`; wire that into
the control layer when a plug graduates from "meter" to "actuator" (e.g. a purifier). Read-only here.

Transport is stamped `wifi-mqtt`; ts is the bridge's UTC receive time (Tasmota's onboard clock may be
unsynced on an air-gapped LAN), matching edge_mapper's stamp-on-ingest behaviour.
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
log = logging.getLogger("ha.tasmota")


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_registry(path: Path) -> dict[str, dict]:
    if not path.exists():
        log.warning("Tasmota registry not found at %s — no devices will be bridged", path)
        return {}
    if yaml is None:
        log.error("pyyaml not installed — cannot read %s", path)
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    out = {}
    for tname, cfg in data.items():
        cfg = cfg or {}
        out[str(tname)] = {
            "device_id": cfg.get("device_id", str(tname)),
            "area": cfg.get("area", "unknown"),
            "device_type": cfg.get("device_type", "power_plug"),
        }
    return out


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def energy_metrics(energy: dict) -> dict:
    """Map a Tasmota ENERGY block -> canonical metric names (only numeric fields that are present)."""
    m = {
        "power_w":          _num(energy.get("Power")),
        "apparent_va":      _num(energy.get("ApparentPower")),
        "power_factor":     _num(energy.get("Factor")),
        "voltage_v":        _num(energy.get("Voltage")),
        "current_a":        _num(energy.get("Current")),
        "energy_kwh":       _num(energy.get("Total")),
        "energy_today_kwh": _num(energy.get("Today")),
    }
    return {k: v for k, v in m.items() if v is not None}


def state_metrics(state: dict) -> dict:
    """Map a Tasmota STATE block -> relay on/off + wifi signal."""
    m = {}
    power = state.get("POWER")                         # single-relay plugs report POWER: ON|OFF
    if isinstance(power, str):
        m["relay_on"] = 1.0 if power.upper() == "ON" else 0.0
    wifi = state.get("Wifi") or {}
    sig = _num(wifi.get("Signal"))                     # dBm (Tasmota also has RSSI 0-100; Signal is dBm)
    if sig is not None:
        m["wifi_rssi_dbm"] = sig
    return m


class TasmotaBridge:
    def __init__(self, registry: dict[str, dict], client: mqtt.Client):
        self._registry = registry
        self._mqtt = client
        self._unknown: set[str] = set()

    def on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            client.subscribe([("tele/+/SENSOR", 0), ("tele/+/STATE", 0)])
            log.info("connected; subscribed to tele/+/SENSOR + tele/+/STATE (%d device(s))",
                     len(self._registry))
        else:
            log.error("MQTT connect failed rc=%s", rc)

    def on_message(self, client, userdata, msg: mqtt.MQTTMessage):
        parts = msg.topic.split("/")                   # tele/<tname>/<kind>
        if len(parts) != 3:
            return
        tname, kind = parts[1], parts[2]
        reg = self._registry.get(tname)
        if not reg:
            if tname not in self._unknown:
                self._unknown.add(tname)
                log.warning("telemetry from UNKNOWN Tasmota topic %r — add it to the registry", tname)
            return
        try:
            payload = json.loads(msg.payload.decode())
        except (ValueError, UnicodeDecodeError) as exc:
            log.warning("bad Tasmota payload on %s: %s", msg.topic, exc)
            return

        if kind == "SENSOR":
            metrics = energy_metrics(payload.get("ENERGY") or {})
        elif kind == "STATE":
            metrics = state_metrics(payload)
        else:
            return
        if not metrics:
            return

        out_topic = f"home/{reg['area']}/{reg['device_id']}/state"
        out = {
            "schema": MESSAGE_SCHEMA,
            "device_id": reg["device_id"],
            "device_type": reg["device_type"],
            "area": reg["area"],
            "ts": _utc_now(),
            "transport": "wifi-mqtt",
            "metrics": metrics,
            "meta": {"tasmota_topic": tname},
        }
        self._mqtt.publish(out_topic, json.dumps(out), qos=1, retain=True)
        log.debug("mapped %s/%s -> %s %s", tname, kind, out_topic, metrics)


def main() -> None:
    p = argparse.ArgumentParser(description="Tasmota → canonical-topic bridge")
    p.add_argument("--registry", default="instance/tasmota-devices.yaml", type=Path)
    p.add_argument("--broker", default=BROKER_HOST)
    p.add_argument("--broker-port", default=BROKER_PORT, type=int)
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s", stream=sys.stdout)

    registry = load_registry(args.registry)
    log.info("Tasmota registry: %d device(s)", len(registry))

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    apply_credentials(client)
    bridge = TasmotaBridge(registry, client)
    client.on_connect = bridge.on_connect
    client.on_message = bridge.on_message
    client.connect(args.broker, args.broker_port, keepalive=60)
    client.loop_forever()


if __name__ == "__main__":
    main()
