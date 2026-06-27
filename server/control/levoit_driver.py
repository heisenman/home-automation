"""Levoit / ESPHome air-purifier driver + issuer Transport (board levoit-integration, mirrors ADR-0011).

The Levoit Vital 200S reflashed to local ESPHome (provisioning/levoit/) is a plain-MQTT WiFi appliance —
not a BLE node and not signed-command-capable. Like the Midea dehumidifier (a trusted LAN local-driver),
its Transport translates the issuer's trait/action/args into the device's native control and reports the
resulting state back for closed-loop reconciliation. The command's HMAC is NOT re-verified here (that's for
untrusted nodes); the issuer already authorized it, and reachability is topological (LAN broker only).

Unlike Midea (a CLI driver), control here is MQTT: publish to the ESPHome `<name>/.../command` topic, then
read the device's echoed `<name>/.../state` to confirm what actually took (the purifier may clamp/ignore a
speed) — so the issuer's intended-vs-reported match is honest.

Trait → ESPHome mapping (the live levoit-office layout, provisioning/levoit/README.md):
    switchable {on}     -> fan/fan/command  ON|OFF       , reported {"on": <bool>}
    ranged     {level}  -> fan/fan/speed_level/command N , reported {"level": <int>}
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from . import protocol

log = logging.getLogger("ha.control.levoit")

# trait -> (arg key, command subtopic, payload(value)->str, state subtopic, parse(str)->typed value)
_TRAIT_MAP = {
    "switchable": ("on",    "fan/fan/command",             lambda v: "ON" if v else "OFF",
                   "fan/fan/state",             lambda s: s.strip().upper() == "ON"),
    "ranged":     ("level", "fan/fan/speed_level/command", lambda v: str(int(v)),
                   "fan/fan/speed_level/state", lambda s: int(float(s))),
}


def load_levoit_devices(registry_path: Path) -> dict[str, str]:
    """Read the bridge registry (esphome_name -> {device_id,...}) and return the INVERSE the transport
    needs: {device_id: esphome_name (MQTT topic prefix)}. Same file the bridge uses, so one source."""
    try:
        import yaml
    except ImportError:                                   # pragma: no cover
        return {}
    if not registry_path.exists():
        return {}
    data = yaml.safe_load(registry_path.read_text()) or {}
    out = {}
    for name, cfg in data.items():
        cfg = cfg or {}
        device_id = cfg.get("device_id", str(name).replace("-", "_"))
        out[device_id] = str(name)
    return out


class LevoitMqttTransport:
    """issuer Transport for local-ESPHome purifiers. `devices` maps our device_id -> ESPHome node name
    (the MQTT topic prefix). Each command opens a short-lived broker connection (like MqttTransport),
    publishes the command, and waits for the device to echo its new state."""
    def __init__(self, devices: dict[str, str], broker: str = "localhost", port: int = 1883,
                 settle_s: float = 3.0):
        import paho.mqtt.client as mqtt                    # lazy, like MqttTransport
        self._mqtt = mqtt
        self.devices = devices
        self.broker, self.port = broker, port
        self.settle_s = settle_s

    def send_and_wait(self, *, node, device_id, area, cmd, now=None, timeout=5.0):
        name = self.devices.get(device_id)
        if name is None:
            return None                                   # not a Levoit → issuer maps to no-ack
        trait, action, args = cmd.get("trait"), cmd.get("action"), cmd.get("args", {})
        m = _TRAIT_MAP.get(trait)
        if m is None or action != "set":
            return protocol.build_ack(cmd_id=cmd["id"], status="rejected",
                                      reason=f"levoit: unsupported {trait}/{action}")
        arg_key, cmd_sub, payfn, state_sub, parse = m
        want = args.get(arg_key)
        cmd_topic = f"{name}/{cmd_sub}"
        state_topic = f"{name}/{state_sub}"

        from ..util.mqtt_creds import apply_credentials   # lazy, mirrors the paho import
        seen: list = []                                   # parsed values from state_topic, in arrival order
        matched = threading.Event()
        lock = threading.Lock()

        def on_msg(c, u, msg):
            try:
                v = parse(msg.payload.decode())
            except Exception:
                return
            with lock:
                seen.append(v)
            if v == want:
                matched.set()

        c = self._mqtt.Client(self._mqtt.CallbackAPIVersion.VERSION2)
        apply_credentials(c)
        c.on_message = on_msg
        deadline = min(timeout, self.settle_s)
        try:
            c.connect(self.broker, self.port, 30)
            c.loop_start()
            c.subscribe(state_topic, qos=0)               # retained current value arrives first
            time.sleep(0.2)                               # let the retained state land (idempotent shortcut)
            with lock:
                already = bool(seen) and seen[-1] == want
            if not already:
                c.publish(cmd_topic, payfn(want), qos=1)
                matched.wait(deadline)
        except OSError as e:
            log.warning("LevoitMqttTransport: broker %s:%s unreachable for %s: %s",
                        self.broker, self.port, device_id, e)
            return None
        finally:
            try:
                c.loop_stop(); c.disconnect()
            except Exception:
                pass

        with lock:
            if not seen:
                return None                               # no state at all (device offline) → no-ack
            reported_val = want if want in seen else seen[-1]   # prefer the confirmed value, else last echo
        return protocol.build_ack(cmd_id=cmd["id"], status="ok",
                                  reported_state={arg_key: reported_val}, source="commanded")
