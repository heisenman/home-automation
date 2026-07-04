"""reTerminal wall-panel driver + issuer Transport (D1001/E1001 as trait-based actuators, ADR-0019 #2).

A wall panel is a plain-MQTT WiFi device that also happens to be a *display* — so, like the Levoit purifier
and the Midea dehumidifier, it is a trusted LAN local-driver, NOT a signed/enrolled node. Its Transport
translates the issuer's trait/action/args into the panel's native `cmd/*` topics and reads the panel's
retained `state/screen` mirror back for honest intended-vs-reported reconciliation (ADR-0014 R3).

INTERIM (unsigned) posture — deliberate: the command's HMAC is NOT verified at the panel (that is for
untrusted enrolled nodes, ADR-0010); the issuer already authorized it and reachability is topological (LAN
broker only). This is posture-consistent with the panel's existing unsigned `cmd/*` surface. When D1001
roadmap #4 (cmd enrollment) lands, promote the panel to the signed MqttTransport and drop this driver.

Trait -> panel mapping (firmware provisioning/reterminal/beachhead, T_SCRC / T_BRTC / T_SCRST):
    switchable {on}    -> cmd/screen      "on"|"off"     , reported {"on": <bool>}
    setpoint   {value} -> cmd/brightness  "N" (0..100)   , reported {"level": <int>}   (0 = screen off)
Both traits read the single retained state topic `state/screen` = {"on":bool,"level":pct}.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from . import protocol

log = logging.getLogger("ha.control.panel")


def _field(name):
    """Parse the panel's retained JSON state ({"on":bool,"level":int}) and pull one field."""
    def parse(s: str):
        return (json.loads(s) or {}).get(name)
    return parse


# trait -> (arg key, command subtopic, payload(value)->str, state subtopic, parse(str)->typed value)
_TRAIT_MAP = {
    "switchable": ("on",    "cmd/screen",     lambda v: "on" if v else "off",
                   "state/screen", _field("on")),
    "setpoint":   ("value", "cmd/brightness", lambda v: str(int(v)),
                   "state/screen", _field("level")),
}


def load_panel_devices(registry_path: Path) -> dict[str, str]:
    """Read the panel side-registry (mqtt_prefix -> {device_id,...}) and return the INVERSE the transport
    needs: {device_id: mqtt_prefix (topic root, e.g. 'd1001-beachhead')}. Mirrors load_levoit_devices."""
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


class PanelMqttTransport:
    """issuer Transport for reTerminal wall panels. `devices` maps our device_id -> panel MQTT topic root.
    Each command opens a short-lived broker connection (like LevoitMqttTransport), publishes to the panel's
    `cmd/*`, and waits for the panel to echo its new `state/screen` so intended-vs-reported is honest."""
    def __init__(self, devices: dict[str, str], broker: str = "localhost", port: int = 1883,
                 settle_s: float = 3.0):
        import paho.mqtt.client as mqtt                    # lazy, like LevoitMqttTransport
        self._mqtt = mqtt
        self.devices = devices
        self.broker, self.port = broker, port
        self.settle_s = settle_s

    def send_and_wait(self, *, node, device_id, area, cmd, now=None, timeout=5.0):
        name = self.devices.get(device_id)
        if name is None:
            return None                                   # not a panel → issuer maps to no-ack
        trait, action, args = cmd.get("trait"), cmd.get("action"), cmd.get("args", {})
        m = _TRAIT_MAP.get(trait)
        if m is None or action != "set":
            return protocol.build_ack(cmd_id=cmd["id"], status="rejected",
                                      reason=f"panel: unsupported {trait}/{action}")
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
            log.warning("PanelMqttTransport: broker %s:%s unreachable for %s: %s",
                        self.broker, self.port, device_id, e)
            return None
        finally:
            try:
                c.loop_stop(); c.disconnect()
            except Exception:
                pass

        with lock:
            if not seen:
                return None                               # no state at all (panel offline) → no-ack
            reported_val = want if want in seen else seen[-1]   # prefer the confirmed value, else last echo
        return protocol.build_ack(cmd_id=cmd["id"], status="ok",
                                  reported_state={arg_key: reported_val}, source="commanded")
