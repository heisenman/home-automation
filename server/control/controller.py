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
from server.control import actuator_state, bootstrap, control_store as store
from server.control.registry import load_control_registry
from server.util.registry_reload import RegistryReloader
from server.control.automation import (
    DEFAULT_SCENE, DeviceState, Override, Policy, Reading, apply_scene, in_window, resolve,
    schedule_off_now)
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
# source_sensor is filled in at seed time with the purifier's OWN device_id (self-sourced), resolved from
# the registry by device_type — never a hardcoded id (ADR-0026: renames flow through with no code change).
LEVOIT_POLICY = {
    "enabled": True,
    "source_sensor": None,                       # set to the purifier's own device_id at seed time
    "control": {"strategy": "threshold_ranged", "metric": "pm25_ugm3",
                "bands": [{"max": 12, "level": 1}, {"max": 35, "level": 2},
                          {"max": 55, "level": 3}, {"max": None, "level": 4}]},
    "schedule": [],
    "defaults": {"running": True},
    "sensor_stale_min": 15,
}

# Which sensor metric the loop drives on. The policy may name it explicitly (control.metric — e.g. an
# air-quality device choosing pm25_ugm3 vs aqi); otherwise it defaults by strategy. Default = RH.
_DEFAULT_METRIC_BY_STRATEGY = {"threshold_ranged": "pm25_ugm3"}
DEFAULT_CONTROL_METRIC = "humidity_pct"


def control_metric(pol: dict) -> str:
    c = (pol or {}).get("control", {}) or {}
    return c.get("metric") or _DEFAULT_METRIC_BY_STRATEGY.get(c.get("strategy"), DEFAULT_CONTROL_METRIC)


class Controller:
    def __init__(self, issuer, drivers: dict, registry: dict, db: str, mqtt_client=None):
        self.issuer = issuer
        self.drivers = drivers                 # device_id -> MideaDriver
        self.registry = registry               # device_id -> DeviceCtl
        self._registry_reloader = None         # optional live control.yaml reload (attach_registry_reloader)
        self.db = db
        self.mqtt = mqtt_client
        self.readings: dict[str, Reading] = {}  # sensor device_id -> latest control Reading
        self.telemetry: dict[str, dict] = {}    # device_id -> {running, fan, ts} for driverless MQTT devices
        self._night_active = False               # night-mode edge state (dusk->LEDs off, dawn->on)
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
        # store ALL numeric metrics for this source + a receive ts; _pick_source extracts whichever metric
        # the consuming device's policy selects (humidity_pct, pm25_ugm3, aqi, …) at tick time.
        nums = {k: float(v) for k, v in metrics.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)}
        if nums:
            with self._lock:
                self.readings[did] = {"m": nums, "ts": time.time()}
        # actuator telemetry for driverless MQTT devices (e.g. Levoit) that have no local-driver status()
        fan_on, fan_speed = metrics.get("fan_on"), metrics.get("fan_speed")
        if fan_on is not None or fan_speed is not None:
            with self._lock:
                self.telemetry[did] = {
                    "running": (bool(fan_on) if fan_on is not None else None),
                    "fan": (int(fan_speed) if fan_speed is not None else None),
                    "ts": time.time(),
                }

    def inject_reading(self, sensor_id: str, value: float, ts: float, metric: str = "humidity_pct"):
        """Test/seed hook. Stores `value` under `metric` (default RH, matching the dehumidifier)."""
        with self._lock:
            self.readings[sensor_id] = {"m": {metric: value}, "ts": ts}

    def _pick_source(self, pol, stale_s, now):
        """Pick the control input: the FIRST FRESH reading of the policy's control metric across
        [source_sensor] + fallback_sensors. If none are fresh, return the first one seen (possibly stale)
        so the resolver fail-safes to default. Returns (Reading|None, used_id|None, via_fallback)."""
        metric = control_metric(pol)
        primary = pol.get("source_sensor")
        order = [primary, *(pol.get("fallback_sensors") or [])]
        first = None
        with self._lock:
            for sid in order:
                if not sid:
                    continue
                rec = self.readings.get(sid)
                if rec is None or metric not in rec["m"]:
                    continue                              # this source doesn't carry the control metric
                r = Reading(rec["m"][metric], rec["ts"])
                if first is None:
                    first = (r, sid)
                if (now - rec["ts"]) <= stale_s:
                    return r, sid, sid != primary
        if first:
            return first[0], first[1], first[1] != primary
        return None, None, False

    # ── tick ────────────────────────────────────────────────────────────────────
    def attach_registry_reloader(self, reloader):
        """Watch control.yaml for changes so an actuator RELOCATE (its area edited in control.yaml) takes
        effect on live control without a restart — the controller is the 6th area-stamper (its self-report
        _publish_state stamps area from self.registry), the sibling of the ingest bridges' devices.yaml
        reload. Only self.registry (area + the indicator loop) is swapped; the issuer/drivers/transports
        are unchanged, so a relocate flows through but adding/removing a device still needs a restart."""
        self._registry_reloader = reloader
        return self

    def _refresh_registry(self):
        if self._registry_reloader is not None:
            self.registry = self._registry_reloader.current()

    def tick(self, now: float | None = None, dry_run: bool = False):
        self._refresh_registry()               # pick up a live control.yaml edit (actuator relocate)
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
            self._apply_night_mode(conn, tod, dry_run)          # LED night mode (all indicator devices)
        finally:
            conn.close()

    def _apply_night_mode(self, conn, tod, dry_run):
        """Edge-triggered LED night mode: at dusk (entering the window) set every indicator-capable device's
        LED OFF; at dawn (leaving) set it ON. Between edges it does nothing, so manual LED toggles are free
        during the day. Disabled -> never touches LEDs. Reuses the schedule in_window helper."""
        nm = store.get_setting(conn, "night_mode") or {}
        if not nm.get("enabled"):
            self._night_active = False
            return
        try:
            night = in_window(tod, nm.get("window") or "22:00-07:00")
        except Exception:
            return                                              # malformed window -> ignore
        if night == self._night_active:
            return                                              # no dusk/dawn edge
        self._night_active = night
        want_on = not night
        for device_id, ctl in self.registry.items():
            if "indicator" not in (getattr(ctl, "traits_cfg", {}) or {}):
                continue
            if dry_run:
                continue
            r = self.issuer.issue(device_id=device_id, trait="indicator", action="set", args={"on": want_on})
            store.append_log(conn, device_id, want_on, "night",
                             f"night-mode -> LED {'on' if want_on else 'off'}", True, r.status)
            log.info("night-mode: %s LED -> %s (%s)", device_id, "on" if want_on else "off", r.status)

    def _mode_cfg(self, device_id):
        """If this device declares a `mode` enum trait wired for graceful on/off (run_mode + idle_mode
        naming labels in its `values` map), return the resolved mapping; else None. Lets the controller
        drive the compressor down GRACEFULLY — switch to Set mode — instead of a hard power cut on 'off'
        (Midea dehumidifier, verified live 2026-07-18)."""
        ctl = self.registry.get(device_id)
        mcfg = (getattr(ctl, "traits_cfg", {}) or {}).get("mode") if ctl else None
        if not mcfg:
            return None
        values = mcfg.get("values") or {}
        run_mode, idle_mode = mcfg.get("run_mode"), mcfg.get("idle_mode")
        if run_mode not in values or idle_mode not in values:
            return None                                # mode trait present but not wired for on/off
        return {"run_mode": run_mode, "idle_mode": idle_mode,
                "run_val": int(values[run_mode]), "idle_val": int(values[idle_mode])}

    def _park_value(self, device_id):
        """The setpoint that makes this device INERT while a manual override parks it, or None if the
        device doesn't opt in. Declared on the `setpoint` trait as `park: max | min | <number>`.

        Which end is inert depends on what the device does: a dehumidifier idles when its target RH is
        at the TOP of the range (nothing to remove), a humidifier or heater when it's at the BOTTOM. So
        the direction is config, not code — see instance/control.yaml. Without this, a graceful 'off'
        only switches the appliance to its idle MODE, where it keeps self-regulating to whatever target
        it was left at: the Midea sat at target 35% and went right on running (found live 2026-08-02)."""
        ctl = self.registry.get(device_id)
        cfg = (getattr(ctl, "traits_cfg", {}) or {}).get("setpoint") if ctl else None
        if not cfg:
            return None
        park = cfg.get("park")
        if park is None:
            return None                                # setpoint device that has not opted in
        lo, hi = cfg.get("min"), cfg.get("max")
        if park == "max":
            return None if hi is None else float(hi)
        if park == "min":
            return None if lo is None else float(lo)
        try:
            v = float(park)
        except (TypeError, ValueError):
            log.warning("%s: bad setpoint park %r (want max|min|<number>)", device_id, park)
            return None
        if lo is not None:
            v = max(v, float(lo))
        if hi is not None:
            v = min(v, float(hi))
        return v

    def _apply_setpoint_park(self, conn, device_id, res, st, now, dry_run):
        """Park/restore the setpoint around a manual override — the other half of a graceful 'off'.

        Evaluated EVERY tick from the resolved state rather than only on transitions, so it is idempotent
        and self-heals: a controller restart mid-pause, or a park command that failed to land, is fixed on
        the next tick instead of leaving the device parked forever. The pre-park setpoint lives in
        control.db (store.get_park), so restore survives a restart and rides the standby snapshot."""
        target = self._park_value(device_id)
        if target is None:
            return                                     # device hasn't opted into parking
        saved = store.get_park(conn, device_id)
        want_park = res.source == "override" and not res.running
        cur = st.get("target")
        if want_park:
            if saved is None:
                if cur is None:
                    return                             # no reading of the current setpoint — try next tick
                if float(cur) == target:
                    return                             # already at the park value; nothing to remember
                if not dry_run:
                    store.set_park(conn, device_id, float(cur), now)
                saved = float(cur)
            desired = target
        else:
            if saved is None:
                return                                 # not parked, nothing to restore
            desired = float(saved)
        if cur is not None and float(cur) == desired:
            if not want_park and not dry_run:
                store.clear_park(conn, device_id)      # restore already true — drop the row
            return
        if dry_run:
            log.info("%s: setpoint park %s -> %s (dry-run)", device_id, cur, desired)
            return
        r = self.issuer.issue(device_id=device_id, trait="setpoint", action="set",
                              args={"value": desired})
        log.info("%s: setpoint %s -> %s (%s) status=%s", device_id, cur, desired,
                 "park" if want_park else "restore", r.status)
        if r.status == "ok" and not want_park:
            store.clear_park(conn, device_id)          # only forget the original once it is back on

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
        # For a graceful-mode device, "running" (for hysteresis + cycle gating) means actively
        # dehumidifying = the appliance is in its run_mode (Continuous), NOT merely powered — so the
        # rule drives the MODE (Set<->Continuous) and the compressor spins down gracefully on 'off'.
        mcfg = self._mode_cfg(device_id)
        if mcfg and st.get("mode") is not None:
            running_now = int(st["mode"]) == mcfg["run_val"]
        else:
            running_now = bool(st.get("running"))
        dev_state = DeviceState(running=running_now, interlocks=tuple(interlocks),
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
            elif mcfg is not None:
                # graceful on/off via operating mode: ON -> run_mode (Continuous, actively dehumidify),
                # OFF -> idle_mode (Set, compressor idles at its setpoint and spins down gracefully).
                # The appliance stays powered either way — never a hard compressor cut from automation.
                if res.running and not bool(st.get("running")):
                    self.issuer.issue(device_id=device_id, trait="switchable", action="set",
                                      args={"on": True})
                target_mode = mcfg["run_mode"] if res.running else mcfg["idle_mode"]
                result = self.issuer.issue(device_id=device_id, trait="mode", action="set",
                                           args={"mode": target_mode})
            else:
                result = self.issuer.issue(device_id=device_id, trait="switchable", action="set",
                                           args={"on": res.running})
            status = result.status
            if result.status == "ok" and res.running != dev_state.running:
                store.record_transition(conn, device_id, res.running, now)
            self._emit(device_id, transport, ev.from_issue_status(result.status), res.reason)
        elif res.act and dry_run:
            status = "dry-run"
        # The other half of a graceful off: idle MODE alone leaves the appliance self-regulating to its
        # old target, so a manual override also parks the setpoint at its inert end (and restores it when
        # the override ends). Runs every tick, not just on a transition — see _apply_setpoint_park.
        self._apply_setpoint_park(conn, device_id, res, st, now, dry_run)
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
        metrics = {}
        if "humidity" in st:
            metrics["humidity_pct"] = st["humidity"]           # ONBOARD = non-authoritative
        if "temp" in st:
            metrics["temperature_c"] = st["temp"]
        if "target" in st:
            metrics["target_humidity_pct"] = st["target"]      # device setpoint (telemetry, for the UI)
        if "fan" in st:
            metrics["fan_speed"] = st["fan"]                   # current fan level
        if "mode" in st:
            metrics["mode"] = st["mode"]                       # operating mode (Set=1/Continuous=2/Dry=4)
        # ADR-0027: single shared stamp. area from the reload-aware control registry (self.registry, kept
        # fresh by _refresh_registry each tick); the helper stamps a fresh ts per call (the writer keys on
        # (device_id, ts, metric), so a stale ts would collide and freeze onboard RH). writer reads area
        # from the PAYLOAD, not the topic — the helper puts it in both.
        topic, payload = actuator_state.actuator_state(
            device_id=device_id, device_type="dehumidifier",
            area=actuator_state.resolve_area(self.registry, device_id),
            metrics=metrics, transport="midea-lan", meta={"authoritative": False},
            extra={"running": st.get("running"), "target_pct": st.get("target"),
                   "mode": st.get("mode")})
        try:
            self.mqtt.publish(topic, json.dumps(payload), qos=0)
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
    # First-run policy seeds are keyed to the CONFIGURED actuator ids (resolved by device_type in
    # control.yaml), not hardcoded strings — so a rename/relocate via the maintenance tools flows through
    # with no code edit, and the controller never re-seeds an old id back into control.db (ADR-0026).
    midea_id = bootstrap.midea_device_id_of(registry)
    levoit_id = next((d for d, c in registry.items()
                      if getattr(c, "device_type", None) == "air_purifier"), None)
    if midea_id:
        store.seed_policy(conn, midea_id, DEFAULT_POLICY)
    if levoit_id:                                    # only seed if the purifier is registered on this box
        store.seed_policy(conn, levoit_id, {**LEVOIT_POLICY, "source_sensor": levoit_id})
    conn.close()
    ctrl = Controller(issuer, drivers, registry, a.db)
    # live-reload control.yaml so an actuator relocate (area edit) takes effect without a controller
    # restart — the control-plane sibling of the ingest bridges' devices.yaml reload.
    ctrl.attach_registry_reloader(RegistryReloader(Path("instance/control.yaml"),
                                                   load_control_registry, logger=log))
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
