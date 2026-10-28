"""Venstar ColorTouch thermostat driver — issuer Transport (ADR-0043). A `local-driver` Node (LAN HTTP, like the
Midea), surfacing TWO entities that share it:

    device_type thermostat   mode {mode}  -> POST mode + both current setpoints     reported {"mode": n}
                             setpoint {channel heat|cool, value}                     reported {"channel", "value"}
    device_type hvac_fan     switchable {on} -> POST fan=1 (on) | fan=0 (auto)       reported {"on": fan setting}

Every command re-reads /query/info first (the wall unit, its schedule, or a person may have changed it since
the last poll) and reports the state READ BACK after the call, so the issuer's intended-vs-reported check is
honest. The issuer has already authorised the command; reachability is topological (air-gap LAN only).
Credentials: instance/venstar.env (docs/SECRETS.md).
"""
from __future__ import annotations

import logging
import time

from . import protocol
from .venstar_api import VenstarClient, VenstarError, plan_fan, plan_mode, plan_setpoint, state_messages

log = logging.getLogger("ha.control.venstar")
DEVICE_TYPES = ("thermostat", "hvac_fan")
READBACK_DELAY_S = 1.0          # the unit applies a POST before answering, but give its info a beat to settle


def venstar_devices_of(registry: dict) -> dict[str, str]:
    """{device_id: device_type} for the Venstar entities in control.yaml (routed by type, ADR-0026)."""
    return {d: c.device_type for d, c in registry.items() if getattr(c, "device_type", None) in DEVICE_TYPES}


def client_from_env(env: dict) -> VenstarClient | None:
    host = env.get("VENSTAR_HOST")
    if not host:
        return None
    return VenstarClient(host, proto=env.get("VENSTAR_PROTO", "http"), user=env.get("VENSTAR_USER") or None,
                         password=env.get("VENSTAR_PASS") or None, pin=env.get("VENSTAR_PIN") or None)


class VenstarTransport:
    def __init__(self, devices: dict[str, str], client: VenstarClient, setpoint_units: dict[str, str],
                 readback_delay_s: float = READBACK_DELAY_S, areas: dict[str, str] | None = None,
                 publish=None):
        """`publish([(topic, payload)])`, if given, gets the command's readback as canonical state messages —
        the bridge only polls every 30 s, so without it the PWA reads the OLD state right after a command
        (Hugh, 2026-10-27: fan set On, the card still said Auto). `areas` = {device_id: area} for the topics."""
        self.devices, self.client = devices, client
        self.areas = areas or {}
        self.publish = publish
        self.units = setpoint_units                       # device_id -> the setpoint trait's unit (degF|degC)
        self.delay = readback_delay_s

    def send_and_wait(self, *, node, device_id, area, cmd, now=None, timeout=5.0):
        kind = self.devices.get(device_id)
        if kind is None:
            return None
        trait, action, args = cmd.get("trait"), cmd.get("action"), cmd.get("args", {})

        def reject(reason):
            return protocol.build_ack(cmd_id=cmd["id"], status="rejected", reason=f"venstar: {reason}")

        try:
            info = self.client.info()
            if kind == "hvac_fan" and trait == "switchable" and action == "set":
                params, read = plan_fan(bool(args.get("on"))), lambda i: {"on": i.get("fan") == 1}
            elif kind == "thermostat" and trait == "mode" and action == "set":
                params, read = plan_mode(info, int(args["mode"])), lambda i: {"mode": i.get("mode")}
            elif kind == "thermostat" and trait == "setpoint" and action == "set":
                ch = args.get("channel")
                params = plan_setpoint(info, ch, args.get("value"), self.units.get(device_id, "degF"))
                read = lambda i: {"channel": ch, "value": float(i.get("heattemp" if ch == "heat" else "cooltemp"))}
            else:
                return reject(f"unsupported {trait}/{action} on a {kind}")
            self.client.control(params)
            time.sleep(self.delay)
            after = self.client.info()
            reported = read(after)
        except VenstarError as e:
            return reject(str(e))
        except (OSError, ValueError, KeyError) as e:      # unreachable / malformed -> no-ack, never a crash
            log.warning("venstar %s: %s", device_id, e)
            return None
        if self.publish is not None:
            ents = {d: (t, self.areas[d]) for d, t in self.devices.items() if d in self.areas}
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            try:
                self.publish(state_messages(after, ents, ts))
            except Exception as e:                        # telemetry is best-effort; the command succeeded
                log.warning("venstar readback publish failed: %s", e)
        return protocol.build_ack(cmd_id=cmd["id"], status="ok", reported_state=reported, source="commanded")


def mqtt_state_publisher(broker: str, port: int):
    """A `publish` for VenstarTransport: one short-lived broker connection per command (like the Levoit
    transport), retained like the bridge's own messages."""
    import json

    def publish(msgs):
        import paho.mqtt.client as mqtt
        from ..util.mqtt_creds import apply_credentials
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        apply_credentials(c)
        c.connect(broker, port, 30)
        c.loop_start()
        try:
            for topic, payload in msgs:
                c.publish(topic, json.dumps(payload), qos=1, retain=True).wait_for_publish(3)
        finally:
            c.loop_stop(); c.disconnect()
    return publish
