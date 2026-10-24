#!/usr/bin/env python3
"""Venstar ColorTouch → canonical-topic bridge (ADR-0043). The thermostat has no push channel, so this POLLS
GET /query/info every --interval seconds and publishes each of its two entities on the canonical topic the
writer and the controller already consume:

    home/<area>/<thermostat id>/state   temperature_c, humidity_pct, heat_setpoint_c, cool_setpoint_c,
                                        hvac_mode (0 off|1 heat|2 cool|3 auto), hvac_state (0 idle|1 heating|
                                        2 cooling|3 lockout|4 error), active_stage, schedule_on, away
    home/<area>/<blower id>/state       running (blower actually on), fan_on_setting (1 on | 0 auto)

The thermostat message is a device self-report → meta.authoritative=false (its own temperature/RH are never an
automatic control source — CONFORMANCE A). Entities come from instance/control.yaml by device_type
(thermostat / hvac_fan); the address + credentials from instance/venstar.env. Unreachable → log + retry; the
recording watch (ADR-0040) surfaces a long silence. ts is the bridge's receive time (like tasmota_bridge).
"""
import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server.control.bootstrap import parse_env_file          # noqa: E402
from server.control.registry import load_control_registry    # noqa: E402
from server.control.venstar_api import VenstarError, info_metrics  # noqa: E402
from server.control.venstar_driver import client_from_env, venstar_devices_of  # noqa: E402

BROKER_HOST = os.environ.get("HA_BROKER_HOST", "127.0.0.1")
BROKER_PORT = int(os.environ.get("HA_BROKER_PORT", "1883"))
log = logging.getLogger("ha.venstar")


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def messages(info: dict, entities: dict[str, tuple[str, str]], ts: str) -> list[tuple[str, dict]]:
    """[(topic, payload)] for one poll. entities: {device_id: (device_type, area)}. Pure."""
    therm, blower = info_metrics(info)
    out = []
    for did, (dtype, area) in sorted(entities.items()):
        m = therm if dtype == "thermostat" else blower
        if not m:
            continue
        out.append((f"home/{area}/{did}/state", {
            "schema": 1, "device_id": did, "device_type": dtype, "area": area, "ts": ts, "transport": "lan-http",
            "metrics": m, "meta": {"node": "venstar", "authoritative": dtype != "thermostat"}}))
    return out


def poll_once(client, entities, publish) -> int:
    msgs = messages(client.info(), entities, _utc_now())
    for topic, payload in msgs:
        publish(topic, json.dumps(payload))
    return len(msgs)


def main() -> None:
    p = argparse.ArgumentParser(description="Venstar thermostat → canonical-topic bridge (polling)")
    p.add_argument("--control-registry", default="instance/control.yaml", type=Path)
    p.add_argument("--env", default="instance/venstar.env", type=Path)
    p.add_argument("--broker", default=BROKER_HOST)
    p.add_argument("--broker-port", default=BROKER_PORT, type=int)
    p.add_argument("--interval", default=30.0, type=float)
    p.add_argument("--once", action="store_true", help="poll once, print the messages, publish nothing")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    a = p.parse_args()
    logging.basicConfig(level=getattr(logging, a.log_level),
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s", stream=sys.stdout)

    reg = load_control_registry(a.control_registry)
    entities = {d: (t, reg[d].area) for d, t in venstar_devices_of(reg).items()}
    client = client_from_env(parse_env_file(a.env))
    if client is None or not entities:
        sys.exit(f"nothing to poll: VENSTAR_HOST in {a.env}? thermostat/hvac_fan entities in {a.control_registry}?")
    if a.once:
        for topic, payload in messages(client.info(), entities, _utc_now()):
            print(topic, json.dumps(payload))
        return

    import paho.mqtt.client as mqtt
    from server.util.mqtt_creds import apply_credentials
    mq = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    apply_credentials(mq)
    mq.connect(a.broker, a.broker_port, keepalive=60)
    mq.loop_start()
    log.info("polling %s every %.0fs for %s", client.base, a.interval, sorted(entities))
    failing = False
    while True:
        try:
            poll_once(client, entities, lambda t, m: mq.publish(t, m, qos=1, retain=True))
            if failing:
                log.info("thermostat reachable again")
            failing = False
        except (OSError, ValueError, VenstarError) as e:
            if not failing:
                log.warning("poll failed: %s (retrying every %.0fs; logged once until it recovers)", e, a.interval)
            failing = True
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
