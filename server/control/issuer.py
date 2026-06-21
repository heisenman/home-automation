"""Command issuer — the single Policy Enforcement Point (ADR-0001, plan §10/§13.2).

Humans/clients never touch devices; every command request flows through here. The issuer:
  1. resolves the device (control registry) + its per-device secret;
  2. evaluates policy (guardrails, mode, command authorization) — deny stops here, nothing is sent;
  3. signs the command with the per-device secret (protocol handshake) + freshness nonce;
  4. sends it via a Transport and awaits the ack;
  5. reconciles intended vs reported state (closed loop, plan §13.6) and audits.

Transport is injected: LoopbackTransport (in-process SimActuator, for tests/demos) or MqttTransport
(real broker). The PEP logic is identical either way.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from . import protocol, traits
from .policy import Decision, PolicyStore
from .sim import SimActuator

log = logging.getLogger("ha.control.issuer")


@dataclass
class DeviceCtl:
    device_id: str
    node: str
    area: str
    traits_cfg: dict[str, dict[str, Any]]


@dataclass
class Result:
    status: str                       # "ok" | "rejected" | "no-ack" | "mismatch" | "unknown-device"
    reason: str
    intended: dict[str, Any] = field(default_factory=dict)
    reported: dict[str, Any] = field(default_factory=dict)
    cmd_id: str = ""


class Transport(Protocol):
    def send_and_wait(self, *, node: str, device_id: str, area: str, cmd: dict[str, Any],
                      now: float | None = None, timeout: float = 5.0) -> dict[str, Any] | None:
        ...


class LoopbackTransport:
    """In-process transport: routes a command straight to a SimActuator and returns its ack.
    For tests/demos — no broker, fully deterministic with injected `now`."""
    def __init__(self, actuators: dict[str, SimActuator]):
        self.actuators = actuators

    def send_and_wait(self, *, node, device_id, area, cmd, now=None, timeout=5.0):
        act = self.actuators.get(device_id)
        if act is None:
            return None
        return act.handle_command(cmd, now=now)


class MqttTransport:
    """Production transport: publish the signed command to home/<area>/<device_id>/cmd and await the
    ack on .../cmd/ack, correlated by command id. paho is imported lazily so the pure-logic PEP and
    its tests need no broker/dependency."""
    def __init__(self, broker: str = "localhost", port: int = 1883,
                 topic_for: Callable[[str, str], str] | None = None):
        import paho.mqtt.client as mqtt  # lazy
        self._mqtt = mqtt
        self.broker, self.port = broker, port
        # topic_for(area, device_id) -> base topic; default home/<area>/<device_id>
        self.topic_for = topic_for or (lambda area, dev: f"home/{area}/{dev}")
        self._acks: dict[str, dict] = {}

    def send_and_wait(self, *, node, device_id, area, cmd, now=None, timeout=5.0):
        import json, threading
        base = self.topic_for(area, device_id)
        cmd_topic, ack_topic = f"{base}/cmd", f"{base}/cmd/ack"
        got = threading.Event()
        result: dict = {}

        def on_msg(c, u, m):
            try:
                a = json.loads(m.payload.decode())
            except Exception:
                return
            if a.get("id") == cmd["id"]:
                result.update(a); got.set()

        c = self._mqtt.Client(self._mqtt.CallbackAPIVersion.VERSION2)
        c.on_message = on_msg
        c.connect(self.broker, self.port, 30)
        c.loop_start()
        c.subscribe(ack_topic, qos=1)
        c.publish(cmd_topic, json.dumps(cmd), qos=1)
        got.wait(timeout)
        c.loop_stop(); c.disconnect()
        return result or None


class CommandIssuer:
    def __init__(self, *, registry: dict[str, DeviceCtl], secrets: dict[str, str],
                 policy: PolicyStore, transport: Transport,
                 mode_getter: Callable[[], str] = lambda: "Normal"):
        self.registry = registry
        self.secrets = secrets
        self.policy = policy
        self.transport = transport
        self.mode_getter = mode_getter
        self._last_cmd_ts: dict[tuple[str, str], float] = {}   # (device, trait) -> ts

    def issue(self, *, device_id: str, trait: str, action: str, args: dict[str, Any] | None = None,
              confirmed: bool = False, now: float | None = None, timeout: float = 5.0) -> Result:
        args = args or {}
        dev = self.registry.get(device_id)
        secret = self.secrets.get(device_id)
        if dev is None or secret is None:
            return Result("unknown-device", f"no control config for '{device_id}'")

        mode = self.mode_getter()
        key = (device_id, trait)
        decision: Decision = self.policy.evaluate(
            device_id=device_id, traits_cfg=dev.traits_cfg, trait=trait, action=action, args=args,
            mode=mode, confirmed=confirmed, now=now, last_cmd_ts=self._last_cmd_ts.get(key),
        )
        if not decision.allow:
            log.info("DENY %s %s/%s: %s", device_id, trait, action, decision.reason)
            return Result("rejected", decision.reason)

        sensitive = traits.get_trait(trait).is_sensitive(action)
        cmd = protocol.build_command(
            device=device_id, node=dev.node, trait=trait, action=action,
            args=decision.normalized_args, secret=secret, sensitive=sensitive, ts=now,
        )
        ack = self.transport.send_and_wait(node=dev.node, device_id=device_id, area=dev.area,
                                           cmd=cmd, now=now, timeout=timeout)
        self._last_cmd_ts[key] = now if now is not None else __import__("time").time()

        if ack is None:
            return Result("no-ack", "device did not ack", intended=decision.normalized_args,
                          cmd_id=cmd["id"])
        if ack.get("status") != "ok":
            return Result("rejected", ack.get("reason", "device-rejected"),
                          intended=decision.normalized_args, reported=ack.get("reported_state", {}),
                          cmd_id=cmd["id"])

        reported = ack.get("reported_state", {})
        # closed loop: did the reported state move to what we intended?
        intended = decision.normalized_args
        matched = all(reported.get(k) == v for k, v in intended.items())
        return Result("ok" if matched else "mismatch",
                      "ok" if matched else "reported state != intended",
                      intended=intended, reported=reported, cmd_id=cmd["id"])
