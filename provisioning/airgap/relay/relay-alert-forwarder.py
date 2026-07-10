#!/usr/bin/env python3
"""relay-alert-forwarder — .210 side (ADR-0033 Phase 3, B1). THE off-network-notification toggle.

Subscribes `relay/alert/out` on the dedicated relay broker (ha-2's mirrored alerts) and — ONLY when
RELAY_NTFY_EGRESS is truthy — republishes them to the LOCAL .210 broker's `home/_alert/new`, where the
existing `ha-ntfy-bridge` turns them into a phone push. Default OFF = on-network-only (Hugh's preference);
flip the env (or enable this service) for off-network push. Directionality-separated from relayd.

Env (/etc/ha-relay/relay.env):
  RELAY_BROKER_HOST (localhost) RELAY_BROKER_PORT (1884) RELAY_BROKER_USER (relayd) RELAY_BROKER_PASS
  HA_LOCAL_BROKER (127.0.0.1) HA_LOCAL_PORT (1883)
  RELAY_NTFY_EGRESS (0)   # <- the toggle; 1/true/on = forward to ntfy path
"""
from __future__ import annotations
import os, sys, logging
import paho.mqtt.client as mqtt

LOG = logging.getLogger("relay-alert-forwarder")


def _truthy(v): return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _mk(cid):
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
    except (AttributeError, TypeError):
        return mqtt.Client(client_id=cid)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    egress = _truthy(os.environ.get("RELAY_NTFY_EGRESS", "0"))
    rhost = os.environ.get("RELAY_BROKER_HOST", "localhost")
    rport = int(os.environ.get("RELAY_BROKER_PORT", "1884"))
    ruser = os.environ.get("RELAY_BROKER_USER", "relayd")
    rpass = os.environ.get("RELAY_BROKER_PASS", "")
    lhost = os.environ.get("HA_LOCAL_BROKER", "127.0.0.1")
    lport = int(os.environ.get("HA_LOCAL_PORT", "1883"))

    local = _mk("relay-alert-fwd-local")
    if egress:
        local.connect(lhost, lport, keepalive=30)
        local.loop_start()

    def on_msg(_c, _u, msg):
        if not egress:
            LOG.info("alert received; egress OFF (on-network-only) — dropped")
            return
        local.publish("home/_alert/new", msg.payload, qos=1)
        LOG.info("forwarded alert -> local home/_alert/new -> ntfy (%d bytes)", len(msg.payload))

    src = _mk("relay-alert-fwd")
    if ruser:
        src.username_pw_set(ruser, rpass)
    src.on_connect = lambda c, *_a: c.subscribe("relay/alert/out", qos=1)
    src.on_message = on_msg
    src.connect(rhost, rport, keepalive=30)
    LOG.info("alert forwarder up: relay/alert/out egress=%s", "ON" if egress else "OFF (default)")
    src.loop_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
