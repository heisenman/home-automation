#!/usr/bin/env python3
"""relay-alert-egress — ha-2 side (ADR-0033 Phase 3, B1).

Mirrors ha-2's alert events (`home/_alert/new` on ha-2's own broker) onto the relay bus
(`relay/alert/out` on the dedicated relay broker at .210). The .210 forwarder decides whether to actually
push them out to ntfy (RELAY_NTFY_EGRESS, default OFF) — so ha-2 always offers its alerts but the
on-/off-network policy lives at the egress point, per the design.

Env (instance/relay.env):
  HA_LOCAL_BROKER (127.0.0.1)  HA_LOCAL_PORT (1883)
  RELAY_BROKER_HOST (192.168.1.245) RELAY_BROKER_PORT (1884)
  RELAY_BROKER_USER (ha2) RELAY_BROKER_PASS
"""
from __future__ import annotations
import os, sys, logging
import paho.mqtt.client as mqtt

LOG = logging.getLogger("relay-alert-egress")


def _mk(client_id):
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except (AttributeError, TypeError):
        return mqtt.Client(client_id=client_id)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    rhost = os.environ.get("RELAY_BROKER_HOST", "192.168.1.245")
    rport = int(os.environ.get("RELAY_BROKER_PORT", "1884"))
    ruser = os.environ.get("RELAY_BROKER_USER", "ha2")
    rpass = os.environ.get("RELAY_BROKER_PASS", "")
    lhost = os.environ.get("HA_LOCAL_BROKER", "127.0.0.1")
    lport = int(os.environ.get("HA_LOCAL_PORT", "1883"))

    out = _mk("relay-alert-egress-out")
    if ruser:
        out.username_pw_set(ruser, rpass)
    out.connect(rhost, rport, keepalive=30)
    out.loop_start()

    def on_msg(_c, _u, msg):
        out.publish("relay/alert/out", msg.payload, qos=1)
        LOG.info("mirrored alert -> relay/alert/out (%d bytes)", len(msg.payload))

    src = _mk("relay-alert-egress-in")
    src.on_connect = lambda c, *_a: c.subscribe("home/_alert/new", qos=1)
    src.on_message = on_msg
    src.connect(lhost, lport, keepalive=30)
    LOG.info("egress bridge up: %s:%d home/_alert/new -> %s:%d relay/alert/out", lhost, lport, rhost, rport)
    src.loop_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
