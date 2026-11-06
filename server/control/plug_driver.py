"""Smart-plug issuer Transports — remote on/off and a plug-side power-cycle (Hugh, 2026-11-05).

Two kinds of plug feed the house's appliances and infrastructure:

  * PG03V3 plugs on our firmware (edge/esp32c3-plug, ADR-0042), device_type `plug`. Enrolled edge nodes that
    verify every command (ADR-0010), so this transport re-signs in the node's `{p, s}` envelope like the ERV
    and Aprilaire transports. Confirmation is the plug's own readback, `relay_on` on home/edge/<node>/plug/adv.
        switchable set   {on}    -> {"op":"plug_set","on":<bool>}
        switchable cycle {off_s} -> {"op":"plug_cycle","off_s":<n>}    (the PLUG times the restore)
  * Tasmota S31 plugs, device_type `tasmota_plug`. Plain MQTT on the authenticated broker: Tasmota cannot
    verify an HMAC, the same standing as the ERV's existing power-cycle recovery (cmnd/erv_pm/POWER).
    Confirmation is Tasmota's `stat/<topic>/RESULT` {"POWER": "ON"|"OFF"}.
        switchable set   {on}    -> cmnd/<topic>/POWER  ON|OFF
        switchable cycle {off_s} -> cmnd/<topic>/Backlog  "Power 0; Delay <off_s*10>; Power 1"

The power-cycle is ALWAYS plug-side: a plug that feeds the box running this server, or the router on its path,
must turn itself back on after the server has lost it. A server-timed OFF..ON would never send the ON.
Whether a plug may be switched OFF at all is the per-plug power mode (server/control/plug_power.py),
enforced in the API before a command reaches here.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from . import protocol
from .edge_cmd import STATE_DIR
from .erv_driver import CONFIRM_S, send_and_confirm

log = logging.getLogger("ha.control.plug")

CYCLE_MAX_S = 120               # the PG03 firmware's CYCLE_MAX_S; Tasmota Delay is in 0.1 s


def plug_devices_of(registry: dict) -> dict[str, str]:
    """{device_id: node} for every control device of type `plug` (PG03 on our firmware) — by TYPE (ADR-0026)."""
    return {d: c.node for d, c in registry.items() if getattr(c, "device_type", None) == "plug"}


def tasmota_plugs_of(registry: dict) -> dict[str, str]:
    """{device_id: tasmota topic} for every control device of type `tasmota_plug`. The topic defaults to the
    device_id (the house convention: the S31s are named after their device) and may be set per device as
    traits.switchable.tasmota."""
    out = {}
    for d, c in registry.items():
        if getattr(c, "device_type", None) == "tasmota_plug":
            sw = ((getattr(c, "traits_cfg", {}) or {}).get("switchable") or {})
            out[d] = str(sw.get("tasmota") or d)
    return out


def _plan(cmd):
    """(kind, value) for a plug command, or (None, reason)."""
    trait, action, args = cmd.get("trait"), cmd.get("action"), cmd.get("args", {}) or {}
    if trait != "switchable":
        return None, f"plug: unsupported trait {trait}"
    if action == "set":
        return ("set", bool(args.get("on"))), None
    if action == "cycle":
        n = int(args.get("off_s", 10))
        if not 1 <= n <= CYCLE_MAX_S:
            return None, f"plug: off_s must be 1..{CYCLE_MAX_S}"
        return ("cycle", n), None
    return None, f"plug: unsupported switchable/{action}"


class PlugEdgeTransport:
    def __init__(self, devices: dict[str, str], lut: dict, broker: str = "localhost", port: int = 1883,
                 state_dir: Path = STATE_DIR, confirm_s: float = CONFIRM_S):
        import paho.mqtt.client as mqtt
        self._mqtt = mqtt
        self.devices = devices
        self.lut = lut
        self.broker, self.port = broker, port
        self.state_dir = state_dir
        self.confirm_s = confirm_s

    def send_and_wait(self, *, node, device_id, area, cmd, now=None, timeout=5.0):
        if device_id not in self.devices:
            return None
        node = self.devices[device_id]
        plan, why = _plan(cmd)
        if plan is None:
            return protocol.build_ack(cmd_id=cmd["id"], status="rejected", reason=why)
        kind, val = plan
        secret = (self.lut.get(node) or {}).get("cmd_secret")
        if not secret:
            return protocol.build_ack(cmd_id=cmd["id"], status="rejected",
                                      reason=f"plug: no cmd_secret for node {node} in the LUT")
        if kind == "set":
            inner, want = {"op": "plug_set", "on": val}, val
        else:
            inner, want = {"op": "plug_cycle", "off_s": val}, False     # confirmed by the OFF half
        matches = lambda m: m.get("relay_on") is not None and bool(m.get("relay_on")) == want  # noqa: E731
        seen = send_and_confirm(self._mqtt, self.broker, self.port, node, secret, inner,
                                f"home/edge/{node}/plug/adv", matches, max(timeout, self.confirm_s),
                                self.state_dir, device_id)
        if not seen:
            return None                                    # broker down or node silent → issuer reports no-ack
        hit = next((m for m in reversed(seen) if matches(m)), seen[-1])
        if not matches(hit):
            return None
        rep = {"on": bool(hit.get("relay_on"))}
        if kind == "cycle":
            rep["off_s"] = val
        return protocol.build_ack(cmd_id=cmd["id"], status="ok", reported_state=rep, source="commanded")


class TasmotaPlugTransport:
    def __init__(self, devices: dict[str, str], broker: str = "localhost", port: int = 1883,
                 confirm_s: float = CONFIRM_S):
        import paho.mqtt.client as mqtt
        self._mqtt = mqtt
        self.devices = devices
        self.broker, self.port = broker, port
        self.confirm_s = confirm_s

    @staticmethod
    def message(topic: str, kind: str, val) -> tuple[str, str, bool]:
        """(command topic, payload, POWER state that confirms it) — pure, for tests."""
        if kind == "set":
            return f"cmnd/{topic}/POWER", "ON" if val else "OFF", bool(val)
        return f"cmnd/{topic}/Backlog", f"Power 0; Delay {int(val) * 10}; Power 1", False

    def send_and_wait(self, *, node, device_id, area, cmd, now=None, timeout=5.0):
        if device_id not in self.devices:
            return None
        plan, why = _plan(cmd)
        if plan is None:
            return protocol.build_ack(cmd_id=cmd["id"], status="rejected", reason=why)
        kind, val = plan
        topic = self.devices[device_id]
        ctopic, payload, want = self.message(topic, kind, val)
        from ..util.mqtt_creds import apply_credentials
        got = threading.Event()
        seen: list[bool] = []

        def on_msg(c, u, msg):
            try:
                raw = msg.payload.decode().strip()
                p = json.loads(raw) if raw.startswith("{") else {"POWER": raw}
            except Exception:
                return
            st = p.get("POWER") or p.get("POWER1")
            if st in ("ON", "OFF"):
                seen.append(st == "ON")
                if (st == "ON") == want:
                    got.set()

        c = self._mqtt.Client(self._mqtt.CallbackAPIVersion.VERSION2)
        apply_credentials(c)
        c.on_message = on_msg
        try:
            c.connect(self.broker, self.port, 30)
            c.loop_start()
            c.subscribe([(f"stat/{topic}/RESULT", 0), (f"stat/{topic}/POWER", 0)])
            c.publish(ctopic, payload, qos=1).wait_for_publish(5)
            got.wait(max(timeout, self.confirm_s))
        except OSError as e:
            log.warning("tasmota plug %s: broker %s:%s unreachable: %s", device_id, self.broker, self.port, e)
            return None
        finally:
            try:
                c.loop_stop(); c.disconnect()
            except Exception:
                pass
        if not got.is_set():
            return None                                    # no readback → issuer reports no-ack
        rep = {"on": want}
        if kind == "cycle":
            rep["off_s"] = val
        log.warning("PLUG %s %s via %s", device_id, f"power-cycle {val}s" if kind == "cycle" else payload, ctopic)
        return protocol.build_ack(cmd_id=cmd["id"], status="ok", reported_state=rep, source="commanded")
