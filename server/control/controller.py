"""ha-controller — the automation runtime (ADR-0011).

Each tick, for every enabled device: read its live state (interlocks) via the driver, gather the latest
trusted sensor reading + active override + schedule, run the PURE resolver, and — if it says act —
issue the command through the signed/ACL issuer. Persists cycle timestamps + control_log, emits
comms-events, publishes device state (incl the onboard RH as NON-authoritative), and fails safe on
stale/unreachable. The decision logic is automation.resolve(); this is its plumbing.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

from server.comms import events as ev
from server.control import bootstrap, control_store as store
from server.control.automation import (
    DEFAULT_SCENE, DeviceState, Override, Policy, Reading, apply_scene, resolve, schedule_off_now)
from server.control.secret_store import available_master

log = logging.getLogger("ha.controller")

# first-run defaults for the dehumidifier (agreed 2026-06-22: living_room source, ON>=44 / OFF<40).
DEFAULT_POLICY = {
    "enabled": True,
    "source_sensor": "meter_pro_living_room",
    "control": {"strategy": "hysteresis", "on_above": 44, "off_below": 40,
                "min_on_min": 10, "min_off_min": 5},
    "schedule": [],
    "defaults": {"running": False},
    "sensor_stale_min": 10,
}

# first-run defaults for the Levoit purifier (Hugh 2026-06-27: PM2.5 speed-stepping, self-sourced).
LEVOIT_POLICY = {
    "enabled": True,
    "source_sensor": "levoit_office",            # self-sourced PM2.5 (its own bridged reading)
    "control": {"strategy": "threshold_ranged",
                "bands": [{"max": 12, "level": 1}, {"max": 35, "level": 2},
                          {"max": 55, "level": 3}, {"max": None, "level": 4}]},
    "schedule": [],
    "defaults": {"running": True},
    "sensor_stale_min": 15,
}

# control INPUT metric per device_type (the value the resolver consumes). Default = RH (dehumidifier).
CONTROL_METRIC = {"air_purifier": "pm25_ugm3"}
DEFAULT_CONTROL_METRIC = "humidity_pct"


class Controller:
    def __init__(self, issuer, drivers: dict, registry: dict, db: str, mqtt_client=None):
        self.issuer = issuer
        self.drivers = drivers                 # device_id -> MideaDriver
        self.registry = registry               # device_id -> DeviceCtl
        self.db = db
        self.mqtt = mqtt_client
        self.readings: dict[str, Reading] = {}  # sensor device_id -> latest control Reading
        self.telemetry: dict[str, dict] = {}    # device_id -> {running, fan, ts} for driverless MQTT devices
        self._lock = threading.Lock()

    def _conn(self):
        c = sqlite3.connect(self.db)
        store.ensure_schema(c)
        return c

    # ── MQTT sensor intake ──────────────────────────────────────────────────────
    def on_message(self, client, userdata, msg):
        try:
            p = json.loads(msg.payload.decode())
        except Exception:
            return
        did = p.get("device_id")
        if not did:
            return
        metrics = p.get("metrics") or {}
        # control input: the metric this device_type drives on (RH for dehumidifier, PM2.5 for purifier)
        metric = CONTROL_METRIC.get(p.get("device_type") or "", DEFAULT_CONTROL_METRIC)
        val = metrics.get(metric)
        if val is not None:
            with self._lock:
                self.readings[did] = Reading(float(val), time.time())
        # actuator telemetry for driverless MQTT devices (e.g. Levoit) that have no local-driver status()
        fan_on, fan_speed = metrics.get("fan_on"), metrics.get("fan_speed")
        if fan_on is not None or fan_speed is not None:
            with self._lock:
                self.telemetry[did] = {
                    "running": (bool(fan_on) if fan_on is not None else None),
                    "fan": (int(fan_speed) if fan_speed is not None else None),
                    "ts": time.time(),
                }

    def inject_reading(self, sensor_id: str, value: float, ts: float):
        """Test/seed hook."""
        with self._lock:
            self.readings[sensor_id] = Reading(value, ts)

    def _pick_source(self, pol, stale_s, now):
        """Pick the control input: the FIRST FRESH reading across [source_sensor] + fallback_sensors.
        If none are fresh, return the first one seen (possibly stale) so the resolver fail-safes to
        default. Returns (reading|None, used_id|None, via_fallback)."""
        primary = pol.get("source_sensor")
        order = [primary, *(pol.get("fallback_sensors") or [])]
        first = None
        with self._lock:
            for sid in order:
                if not sid:
                    continue
                r = self.readings.get(sid)
                if r is None:
                    continue
                if first is None:
                    first = (r, sid)
                if (now - r.ts) <= stale_s:
                    return r, sid, sid != primary
        if first:
            return first[0], first[1], first[1] != primary
        return None, None, False

    # ── tick ────────────────────────────────────────────────────────────────────
    def tick(self, now: float | None = None, dry_run: bool = False):
        now = now if now is not None else time.time()
        lt = time.localtime(now)
        tod = lt.tm_hour * 60 + lt.tm_min
        conn = self._conn()
        try:
            scene = store.get_scene(conn, DEFAULT_SCENE)        # whole-house Home/Away/Sleep
            for device_id, pol in store.all_policies(conn).items():
                if not pol.get("enabled", True):
                    continue
                self._tick_device(conn, device_id, pol, now, tod, scene, dry_run)
        finally:
            conn.close()

    def _tick_device(self, conn, device_id, pol, now, tod, scene, dry_run):
        drv = self.drivers.get(device_id)
        if drv is not None:
            try:
                st = drv.status()                              # live interlocks + state (local driver)
            except Exception as e:                             # unreachable -> fail safe, don't act
                log.warning("%s status failed: %s", device_id, e)
                self._emit(device_id, "midea-lan", ev.UNREACHABLE, str(e))
                store.append_log(conn, device_id, False, "safety", f"unreachable: {e}", False, "no-status")
                return
            transport = "midea-lan"
        else:
            # driverless MQTT device (e.g. Levoit purifier): state comes from bridged telemetry
            with self._lock:
                tel = dict(self.telemetry.get(device_id) or {})
            if not tel:
                store.append_log(conn, device_id, False, "safety", "no telemetry yet", False, "no-status")
                return
            st = {"running": tel.get("running"), "fan": tel.get("fan")}
            transport = "wifi-mqtt"

        interlocks = []
        if st.get("tank_full"):
            interlocks.append("tank_full")
        if st.get("error"):
            interlocks.append("error")
        last_on, last_off = store.get_cycle(conn, device_id)
        dev_state = DeviceState(running=bool(st.get("running")), interlocks=tuple(interlocks),
                                last_on_ts=last_on, last_off_ts=last_off,
                                level=(int(st["fan"]) if st.get("fan") is not None else None))
        # fold the active house scene into the effective policy (relaxed thresholds and/or force-off)
        eff_pol, scene_off = apply_scene(pol, scene)
        policy = Policy.from_dict(eff_pol)
        sensor, used_id, via_fallback = self._pick_source(pol, policy.sensor_stale_s, now)
        if sensor is not None and (now - sensor.ts) > policy.sensor_stale_s:
            self._emit(device_id, "ble-adv", ev.STALE, f"{used_id} stale")
        ov = store.get_override(conn, device_id, now)
        override = Override(ov[0], ov[1]) if ov else None
        sched_off = schedule_off_now(pol.get("schedule"), tod)

        res = resolve(policy, now, sensor, dev_state, override, sched_off, scene_off, scene)
        reason = res.reason + (f" (via fallback {used_id})" if via_fallback and res.source == "rule" else "")
        status = "noop"
        if res.act and not dry_run:
            if res.level is not None:
                # speed-stepping (ranged): ensure the fan is ON, then set the level
                if res.running and not dev_state.running:
                    self.issuer.issue(device_id=device_id, trait="switchable", action="set",
                                      args={"on": True})
                result = self.issuer.issue(device_id=device_id, trait="ranged", action="set",
                                           args={"level": res.level})
            else:
                result = self.issuer.issue(device_id=device_id, trait="switchable", action="set",
                                           args={"on": res.running})
            status = result.status
            if result.status == "ok" and res.running != dev_state.running:
                store.record_transition(conn, device_id, res.running, now)
            self._emit(device_id, transport, ev.from_issue_status(result.status), res.reason)
        elif res.act and dry_run:
            status = "dry-run"
        store.append_log(conn, device_id, res.running, res.source, reason, res.act, status)
        log.info("%s -> %s | %s | act=%s status=%s%s", device_id,
                 (f"speed {res.level}" if res.level is not None else ("ON" if res.running else "OFF")),
                 res.reason, res.act, status,
                 f" | sensor={sensor.value:.0f}" if sensor else " | sensor=none")
        if drv is not None:                                    # Midea self-reports; bridged devices already publish
            self._publish_state(device_id, st)

    # ── outputs ────────────────────────────────────────────────────────────────
    def _emit(self, device_id, transport, kind, detail):
        if self.mqtt is None:
            return
        try:
            self.mqtt.publish(f"home/_event/{device_id}",
                              json.dumps({"device_id": device_id, "transport": transport,
                                          "kind": kind, "detail": detail, "ts": time.time()}), qos=0)
        except Exception:
            pass

    def _publish_state(self, device_id, st):
        if self.mqtt is None:
            return
        ctl = self.registry.get(device_id)
        area = getattr(ctl, "area", "unknown")
        metrics = {}
        if "humidity" in st:
            metrics["humidity_pct"] = st["humidity"]           # ONBOARD = non-authoritative
        if "temp" in st:
            metrics["temperature_c"] = st["temp"]
        if "target" in st:
            metrics["target_humidity_pct"] = st["target"]      # device setpoint (telemetry, for the UI)
        if "fan" in st:
            metrics["fan_speed"] = st["fan"]                   # current fan level
        # stamp the publish time: the writer keys readings on (device_id, ts, metric), so without a
        # fresh ts every self-report collides on ts="" and INSERT OR IGNORE freezes onboard RH forever.
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        payload = {"schema": 1, "device_id": device_id, "device_type": "dehumidifier", "ts": ts,
                   "transport": "midea-lan", "running": st.get("running"),
                   "target_pct": st.get("target"), "metrics": metrics,
                   "meta": {"authoritative": False}}
        try:
            self.mqtt.publish(f"home/{area}/{device_id}/state", json.dumps(payload), qos=0)
        except Exception:
            pass

    # ── run loop ─────────────────────────────────────────────────────────────────
    def run(self, broker, port, tick_s=45, dry_run=False):
        import paho.mqtt.client as mqtt
        from server.util.mqtt_creds import apply_credentials
        conn = self._conn()
        sources = {p.get("source_sensor") for p in store.all_policies(conn).values()
                   if p.get("source_sensor")}
        conn.close()
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        apply_credentials(c)
        c.on_message = self.on_message

        def on_connect(cl, u, f, rc, props=None):
            for s in sources:
                cl.subscribe(f"home/+/{s}/state", qos=0)
            log.info("subscribed to %d source sensor(s): %s", len(sources), sorted(sources))
        c.on_connect = on_connect
        self.mqtt = c
        c.connect(broker, port, 60)
        c.loop_start()
        log.info("ha-controller running; tick=%ss dry_run=%s", tick_s, dry_run)
        while True:
            try:
                self.tick(dry_run=dry_run)
            except Exception:
                log.exception("tick failed")
            time.sleep(tick_s)


def main():
    ap = argparse.ArgumentParser(description="Home automation controller")
    ap.add_argument("--dry-run", action="store_true", help="decide + log but never issue a command")
    ap.add_argument("--once", action="store_true", help="run a single tick then exit (waits for a sensor)")
    ap.add_argument("--tick-s", type=int, default=int(os.environ.get("HA_CONTROL_TICK_S", "45")))
    ap.add_argument("--db", default=os.environ.get("HA_CONTROL_DB", "instance/db/control.db"))
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, stream=__import__("sys").stdout,
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    master = available_master()
    if not master:
        log.error("no master passphrase — controller cannot build the issuer")
        return
    broker = os.environ.get("HA_BROKER", "localhost")
    port = int(os.environ.get("HA_BROKER_PORT", "1883"))
    issuer, registry, drivers = bootstrap.build_issuer(
        master, control_registry=Path("instance/control.yaml"),
        node_secrets_lut=Path("instance/node_secrets.enc"),
        control_policy=Path("instance/control_policy.yaml"),
        control_secrets=Path("instance/control_secrets.yaml"),
        midea_device_env=Path("instance/midea-device.env"), broker=broker, port=port)

    conn = sqlite3.connect(a.db)
    store.ensure_schema(conn)
    store.seed_policy(conn, "dehumidifier_office", DEFAULT_POLICY)
    if "levoit_office" in registry:                  # only seed if the purifier is registered on this box
        store.seed_policy(conn, "levoit_office", LEVOIT_POLICY)
    conn.close()
    ctrl = Controller(issuer, drivers, registry, a.db)
    if a.once:
        import paho.mqtt.client as mqtt
        from server.util.mqtt_creds import apply_credentials
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        apply_credentials(c)
        c.on_message = ctrl.on_message
        srcs = {p.get("source_sensor") for p in store.all_policies(sqlite3.connect(a.db)).values()
                if p.get("source_sensor")}
        c.on_connect = lambda cl, u, f, rc, props=None: [cl.subscribe(f"home/+/{s}/state") for s in srcs]
        ctrl.mqtt = c
        c.connect(broker, port, 60)
        c.loop_start()
        time.sleep(8)                       # let a sensor reading arrive
        ctrl.tick(dry_run=a.dry_run)
        c.loop_stop()
        return
    ctrl.run(broker, port, tick_s=a.tick_s, dry_run=a.dry_run)


if __name__ == "__main__":
    main()
