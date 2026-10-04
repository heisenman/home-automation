"""Broan ERV issuer Transport — signed RS-485 control through edge node hvac_c6 (ADR-0041, ADR-0010).

Unlike Levoit/Midea (trusted LAN local-drivers), the ERV is driven by an ENROLLED edge node that verifies
every command itself: HMAC with its per-node secret, a freshness window, and strictly increasing (ts, seq).
So this transport re-signs in the firmware's `{p, s}` envelope (server/control/edge_cmd.py) rather than
forwarding the issuer's own command, and publishes to `home/edge/<node>/cmd`.

The node sends no ack. Confirmation is the ERV's OWN state: the node re-reads register 00 20 after a mode
write and publishes `home/edge/<node>/erv/adv` on the change (and every 10 s regardless), carrying
`fan_mode` (the Broan wire value) and `ovr_boost` (the relay's actual pin level). So the issuer's
intended-vs-reported comparison is against what the ERV reports, not what we sent.

Trait mapping (values verified live 2026-10-04, design §7.2 log):
    mode  {mode:<broan int>} -> {"op":"erv_mode","mode":<name>}   reported {"mode": fan_mode}
    timed {minutes:<0..60>}  -> {"op":"erv_boost","min":<n>}      reported {"minutes": n} once ovr_boost == (n>0)
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from . import protocol
from .edge_cmd import STATE_DIR, signed_command

log = logging.getLogger("ha.control.erv")

# Broan fan-mode wire value -> the node's erv_mode name. Must match kModes[] in
# edge/esp32c6-hvac/main/app_main.c and MODES in tools/erv_cmd.py. 0x02 (OVR) is deliberately absent.
ERV_MODE_NAMES = {1: "off", 8: "int", 9: "low", 11: "med", 10: "high", 12: "turbo"}

# A mode change publishes at once (~4 s observed); re-selecting the CURRENT mode produces no change, so it
# confirms only on the node's next periodic publish (10 s). Wait past one period rather than report no-ack.
CONFIRM_S = 12.0


def erv_devices_of(registry: dict) -> dict[str, str]:
    """{device_id: node} for every control device of type `erv` — resolved by TYPE (ADR-0026)."""
    return {d: c.node for d, c in registry.items() if getattr(c, "device_type", None) == "erv"}


class ErvEdgeTransport:
    def __init__(self, devices: dict[str, str], lut: dict, broker: str = "localhost", port: int = 1883,
                 state_dir: Path = STATE_DIR, confirm_s: float = CONFIRM_S):
        import paho.mqtt.client as mqtt                    # lazy, like the other transports
        self._mqtt = mqtt
        self.devices = devices
        self.lut = lut
        self.broker, self.port = broker, port
        self.state_dir = state_dir
        self.confirm_s = confirm_s

    def _plan(self, cmd):
        """(inner op, readback predicate, reported_state builder) or (None, reason)."""
        trait, action, args = cmd.get("trait"), cmd.get("action"), cmd.get("args", {})
        if action != "set":
            return None, f"erv: unsupported {trait}/{action}"
        if trait == "mode":
            want = int(args.get("mode"))
            name = ERV_MODE_NAMES.get(want)
            if name is None:
                return None, f"erv: mode {want} has no node mapping"
            return ({"op": "erv_mode", "mode": name},
                    lambda m: m.get("fan_mode") == want,
                    lambda m: {"mode": m.get("fan_mode")}), None
        if trait == "timed":
            n = int(args.get("minutes"))
            on = n > 0
            return ({"op": "erv_boost", "min": n},
                    lambda m: bool(m.get("ovr_boost")) == on,
                    lambda m: {"minutes": n if bool(m.get("ovr_boost")) == on else None,
                               "on": bool(m.get("ovr_boost"))}), None
        return None, f"erv: unsupported trait {trait}"

    def send_and_wait(self, *, node, device_id, area, cmd, now=None, timeout=5.0):
        if device_id not in self.devices:
            return None
        node = self.devices[device_id]
        plan, why = self._plan(cmd)
        if plan is None:
            return protocol.build_ack(cmd_id=cmd["id"], status="rejected", reason=why)
        inner, matches, reported_of = plan
        secret = (self.lut.get(node) or {}).get("cmd_secret")
        if not secret:
            return protocol.build_ack(cmd_id=cmd["id"], status="rejected",
                                      reason=f"erv: no cmd_secret for node {node} in the LUT")

        from ..util.mqtt_creds import apply_credentials
        seen: list[dict] = []
        matched = threading.Event()
        lock = threading.Lock()

        def on_msg(c, u, msg):
            try:
                m = (json.loads(msg.payload.decode()) or {}).get("metrics") or {}
            except Exception:
                return
            with lock:
                seen.append(m)
            if matches(m):
                matched.set()

        c = self._mqtt.Client(self._mqtt.CallbackAPIVersion.VERSION2)
        apply_credentials(c)
        c.on_message = on_msg
        try:
            c.connect(self.broker, self.port, 30)
            c.loop_start()
            c.subscribe(f"home/edge/{node}/erv/adv", qos=0)
            env = signed_command(node, secret, inner, self.state_dir)
            c.publish(f"home/edge/{node}/cmd", json.dumps(env, separators=(",", ":")), qos=1)
            matched.wait(max(timeout, self.confirm_s))
        except OSError as e:
            log.warning("ErvEdgeTransport: broker %s:%s unreachable for %s: %s",
                        self.broker, self.port, device_id, e)
            return None
        finally:
            try:
                c.loop_stop(); c.disconnect()
            except Exception:
                pass

        with lock:
            if not seen:
                return None                                # node silent → issuer reports no-ack
            hit = next((m for m in reversed(seen) if matches(m)), seen[-1])
        return protocol.build_ack(cmd_id=cmd["id"], status="ok", reported_state=reported_of(hit),
                                  source="commanded")
