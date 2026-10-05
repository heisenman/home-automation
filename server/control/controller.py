"""ha-controller — the automation runtime (ADR-0011).

Each tick, for every enabled device: read its live state (interlocks) via the driver, gather the latest
trusted sensor reading + active override + schedule, run the PURE resolver, and — if it says act —
issue the command through the signed/ACL issuer. Persists cycle timestamps + control_log, emits
comms-events, publishes device state (incl the onboard RH as NON-authoritative), and fails safe on
stale/unreachable. The decision logic is automation.resolve(); this is its plumbing.
"""
from __future__ import annotations

import argparse
import calendar
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
from server.util.psychro import dewpoint_c
from server.util.registry_reload import RegistryReloader
from server.control.automation import (
    DEFAULT_SCENE, DeviceState, Override, Policy, Reading, Resolution, apply_scene, in_window, resolve,
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

# Broan ERV (device_type erv). Seeded DISABLED: the ERV has no sensor of its own, so the operator picks which
# air-quality sensors to average (ADR-0014 R2 — never auto-wired) and enables it in the PWA. Bands run on the
# unified 0-100 index (higher = cleaner) at the ADR-0035 edges, so worse air -> higher level; level N is the
# ERV's mode `levels[N-1]` (low/med/high/turbo). LOW is the floor: automation never turns it off — only an
# explicit Off override / scene / schedule does (Hugh, 2026-10-04).
ERV_POLICY = {
    "enabled": False,
    "source_sensor": None,
    "source_sensors": [],
    "aggregate": "mean",
    "control": {"strategy": "threshold_ranged", "metric": "air_quality",
                "bands": [{"max": 20, "level": 4}, {"max": 40, "level": 3},
                          {"max": 60, "level": 2}, {"max": None, "level": 1}]},
    "schedule": [],
    "defaults": {"running": True},
    "sensor_stale_min": 30,
}

# Aprilaire E070 (device_type dehum) — DISABLED until the operator picks the RH sensors to average (R2).
# Hysteresis on the AVERAGE of those sensors; the call is a lease the controller renews. `ventilation`
# couples it to the ERV it sits in series with (Hugh, 2026-10-04): while dehumidifying the ERV runs at AT
# LEAST `during_level`; after each stop at AT LEAST `after_level` for `after_min` to blow the coil dry. A
# floor, never a ceiling — the ERV runs max(own air-quality level, floor).
DEHUM_POLICY = {
    "enabled": False,
    "source_sensor": None,
    "source_sensors": [],
    "aggregate": "mean",
    "control": {"strategy": "hysteresis", "metric": "humidity_pct", "on_above": 55, "off_below": 50,
                "min_on_min": 10, "min_off_min": 10},
    "ventilation": {"device": None, "during_level": 2, "after_level": 3, "after_min": 15},
    # Skip calling when the OUTDOOR dew point is below min_dewpoint_c (40 °F): that air is so dry that
    # ventilation alone dries the house, and it is where the E070's E8 inlet lockout starts. Outdoor dew point
    # is a deliberately conservative proxy — the unit's real inlet is post-ERV air (in cold weather the ERV
    # adds moisture), so this errs toward skipping. Gates on dew point ONLY: outdoor TEMPERATURE would wrongly
    # block cold days, since the ERV warms the air before the coil. A manual Boost override bypasses it; a
    # stale/missing outdoor reading fails OPEN (the unit protects itself; 'Called but not running' surfaces it).
    "outdoor_gate": {"sensor": None, "min_dewpoint_c": 4.4},
    "schedule": [],
    "defaults": {"running": False},
    "sensor_stale_min": 30,
}
LEASE_RENEW_BELOW_S = 300


def _level_words(text: str, labels: list) -> str:
    """'sensor 76 -> speed 1' -> 'sensor 76 -> low' for a level-mode device (labels = its mode `levels`)."""
    import re

    def word(m):
        n = int(m.group(2))
        return labels[n - 1] if 1 <= n <= len(labels) else m.group(0)
    return re.sub(r"\b(speed|level) (\d+)\b", word, text)      # renew a leased call once less than this is left on it

# Which sensor metric the loop drives on. The policy may name it explicitly (control.metric — e.g. an
# air-quality device choosing pm25_ugm3 vs aqi); otherwise it defaults by strategy. Default = RH.
_DEFAULT_METRIC_BY_STRATEGY = {"threshold_ranged": "pm25_ugm3"}
DEFAULT_CONTROL_METRIC = "humidity_pct"

# Metrics that are DERIVED server-side and therefore never appear in an MQTT /state payload. The
# controller's reading cache is fed exclusively by on_message, so a policy driving on one of these would
# find nothing there and fail safe to its default forever — silently, since "no reading" is
# indistinguishable from "sensor offline". These are resolved out of hot.db instead, where the sampler
# stores them (ha-gas-quality-sampler, every 60s, with its own frozen-input freshness gate).
#
# The stored row's OWN ts is used, so the normal sensor_stale_min logic still governs: if the sampler
# stops writing, the source goes stale and the resolver fails safe exactly as it would for a dead radio.
DERIVED_METRICS = frozenset({"air_quality"})
_DERIVED_MAX_AGE_S = 3600.0      # ignore stored points older than this outright (cheap query bound)


def control_metric(pol: dict) -> str:
    c = (pol or {}).get("control", {}) or {}
    return c.get("metric") or _DEFAULT_METRIC_BY_STRATEGY.get(c.get("strategy"), DEFAULT_CONTROL_METRIC)


class Controller:
    def __init__(self, issuer, drivers: dict, registry: dict, db: str, mqtt_client=None,
                 hot_db: str | None = None):
        self.issuer = issuer
        self.drivers = drivers                 # device_id -> MideaDriver
        self.registry = registry               # device_id -> DeviceCtl
        self._registry_reloader = None         # optional live control.yaml reload (attach_registry_reloader)
        self.db = db
        self.hot_db = hot_db                   # readings store, for DERIVED_METRICS (None = MQTT only)
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
        # leased-call actuators (the Aprilaire) report the call pin + lease remaining
        if metrics.get("dh_call") is not None:
            with self._lock:
                self.telemetry[did] = {"running": bool(metrics["dh_call"]), "fan": None,
                                       "lease_left_s": metrics.get("call_left_s"), "ts": time.time()}
        # level-mode actuators (the ERV) report their MODE; translate it to running + level for the resolver
        if metrics.get("fan_mode") is not None:
            lm = self._level_modes(did)
            if lm is not None:
                fm = int(metrics["fan_mode"])
                with self._lock:
                    self.telemetry[did] = {
                        "running": fm != lm["off_val"],
                        "fan": (lm["vals"].index(fm) + 1) if fm in lm["vals"] else None,
                        "fan_mode": fm, "ts": time.time(),
                    }
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

    def _latest_stored(self, sensor_id: str, metric: str, now: float):
        """Newest stored reading of `metric` for `sensor_id` from hot.db, as {"m": {...}, "ts": epoch} —
        the same shape on_message caches, so _pick_source treats both identically. Only used for
        DERIVED_METRICS, which never reach MQTT. Returns None if absent, unparseable, or older than
        _DERIVED_MAX_AGE_S. Read-only and best-effort: a missing/locked hot.db must never kill a tick."""
        if not self.hot_db:
            return None
        try:
            conn = sqlite3.connect(f"file:{self.hot_db}?mode=ro", uri=True, timeout=2.0)
            try:
                row = conn.execute(
                    "SELECT value, ts FROM readings WHERE device_id=? AND metric=? "
                    "ORDER BY ts DESC LIMIT 1", (sensor_id, metric)).fetchone()
            finally:
                conn.close()
        except Exception:
            log.debug("hot.db lookup failed for %s/%s", sensor_id, metric, exc_info=True)
            return None
        if not row or row[0] is None:
            return None
        # readings.ts is ISO-8601 UTC ('...Z'). timegm, NOT mktime — mktime reads the tuple as LOCAL
        # time, which silently shifts the age by the box's UTC offset and would make a fresh point look
        # hours stale (or a stale one fresh) the moment this runs anywhere but a UTC box.
        try:
            ts = calendar.timegm(time.strptime(str(row[1]), "%Y-%m-%dT%H:%M:%SZ"))
        except Exception:
            return None
        if now - ts > _DERIVED_MAX_AGE_S:
            return None
        return {"m": {metric: float(row[0])}, "ts": float(ts)}

    def _pick_source(self, pol, stale_s, now):
        """Pick the control input: the FIRST FRESH reading of the policy's control metric across
        [source_sensor] + fallback_sensors. If none are fresh, return the first one seen (possibly stale)
        so the resolver fail-safes to default. Returns (Reading|None, used_id|None, via_fallback).

        Sources are resolved from the MQTT cache, EXCEPT for DERIVED_METRICS (air_quality), which are
        computed server-side and never published — those come from hot.db carrying their stored ts, so
        freshness is judged the same way for both."""
        metric = control_metric(pol)
        derived = metric in DERIVED_METRICS
        if pol.get("aggregate") == "mean" and pol.get("source_sensors"):
            return self._mean_source(pol["source_sensors"], metric, derived, stale_s, now)
        primary = pol.get("source_sensor")
        order = [primary, *(pol.get("fallback_sensors") or [])]
        first = None
        with self._lock:
            live = dict(self.readings) if not derived else {}
        for sid in order:
            if not sid:
                continue
            rec = live.get(sid)
            if rec is None or metric not in rec["m"]:
                # derived metrics are never in the cache; go to the stored series instead
                rec = self._latest_stored(sid, metric, now) if derived else None
            if rec is None or metric not in rec["m"]:
                continue                                  # this source doesn't carry the control metric
            r = Reading(rec["m"][metric], rec["ts"])
            if first is None:
                first = (r, sid)
            if (now - rec["ts"]) <= stale_s:
                return r, sid, sid != primary
        if first:
            return first[0], first[1], first[1] != primary
        return None, None, False

    def _mean_source(self, sids, metric, derived, stale_s, now):
        """The MEAN of the fresh readings of `metric` across `sids` (an operator-chosen set — e.g. the rooms
        an ERV ventilates). Stale/missing members are left out, not counted as zero; the result's ts is the
        OLDEST fresh member's, so staleness stays conservative. No fresh member -> the first stale one, so
        the resolver fail-safes exactly as for a single dead source."""
        with self._lock:
            live = dict(self.readings) if not derived else {}
        fresh, first = [], None
        for sid in sids:
            rec = live.get(sid)
            if rec is None or metric not in rec["m"]:
                rec = self._latest_stored(sid, metric, now) if derived else None
            if rec is None or metric not in rec["m"]:
                continue
            if first is None:
                first = (Reading(rec["m"][metric], rec["ts"]), sid)
            if (now - rec["ts"]) <= stale_s:
                fresh.append((rec["m"][metric], rec["ts"]))
        if fresh:
            avg = sum(v for v, _ in fresh) / len(fresh)
            return Reading(avg, min(t for _, t in fresh)), f"mean of {len(fresh)}/{len(sids)}", False
        if first:
            return first[0], first[1], False
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
            pols = store.all_policies(conn)
            # devices that set a ventilation FLOOR (the dehumidifier) tick before the device they floor (the
            # ERV), so the ERV sees this tick's demand rather than the last one
            for device_id, pol in sorted(pols.items(), key=lambda kv: not (kv[1] or {}).get("ventilation")):
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

    # ── device coupling: a dehumidifier in series with an ERV (Hugh, 2026-10-04) ───────────────────────────
    def _track_ventilation_demand(self, device_id, pol, was_running, wants_running, now):
        """Record the floor this device puts on its ventilation partner: `during_level` while it runs (or is
        about to), then `after_level` for `after_min` once it stops (coil dry-out). The floor itself is the
        memory: a 'during' floor still present when the device no longer wants to run IS the stop edge.
        (In-memory: a controller restart mid-dry-out drops the rest of that window — the coil still dries.)"""
        v = pol.get("ventilation") or {}
        partner = v.get("device")
        if not partner:
            return
        floors = self.__dict__.setdefault("_vent_floor", {})
        if wants_running:
            floors[partner] = (int(v.get("during_level", 2)), None, device_id)
        elif partner in floors and floors[partner][1] is None:
            floors[partner] = (int(v.get("after_level", 3)), now + float(v.get("after_min", 15)) * 60,
                               device_id)

    def _ventilation_floor(self, device_id, now):
        f = (self.__dict__.get("_vent_floor") or {}).get(device_id)
        if not f:
            return None
        level, until, source = f
        if until is not None and now >= until:
            self._vent_floor.pop(device_id, None)
            return None
        return level, until, source

    def _apply_ventilation_floor(self, conn, device_id, res, dev_state, override, now, lm):
        """Raise a level-mode device (the ERV) to its partner's floor. A floor never lowers it, and an explicit
        operator OFF override still wins (the partner's interlock then holds the dehumidifier off)."""
        f = self._ventilation_floor(device_id, now)
        if f is None:
            return res
        if override is not None and override.active(now) and override.action == "off":
            return res
        level, until, source = f
        want = max(int(res.level or 0) if res.running else 0, level)
        if res.running and res.level is not None and res.level >= level:
            return res
        why = (f"{source} running -> at least level {level}" if until is None
               else f"{source} coil dry-out -> at least level {level} for {max(0, (until - now) / 60):.0f}m")
        return Resolution(True, dev_state.level != want or not dev_state.running, "rule",
                          f"{res.reason}; {why}", level=want)

    def _apply_outdoor_gate(self, pol, res, dev_state, now):
        g = pol.get("outdoor_gate") or {}
        sid = g.get("sensor")
        if not sid or not res.running or res.source == "override" or dev_state.running:
            return res                                   # gates STARTS only; a running call ends by hysteresis
        with self._lock:
            rec = self.readings.get(sid)
        if rec is None or (now - rec["ts"]) > float(pol.get("sensor_stale_min", 30)) * 60:
            return res                                   # fail open — see DEHUM_POLICY["outdoor_gate"]
        dp = dewpoint_c(rec["m"].get("temperature_c"), rec["m"].get("humidity_pct"))
        lim = float(g.get("min_dewpoint_c", 4.4))
        if dp is None or dp >= lim:
            return res
        return Resolution(False, dev_state.running, "rule",
                          f"{res.reason}; skipped: outdoor dew point {dp:.1f}°C < {lim:.1f}°C "
                          f"(dry outside — ventilation dries the house; E8 territory)")

    def _apply_airflow_interlock(self, conn, device_id, pol, res, dev_state, now):
        """A dehumidifier in series with an ERV must not run on stagnant air. Allowed while the ERV is moving
        air, or when the ERV's own automation will be raised by the floor. Blocked when the ERV is in an
        operator OFF override or its automation is disabled AND it isn't moving."""
        partner = (pol.get("ventilation") or {}).get("device")
        if not partner or not res.running:
            return res
        with self._lock:
            tel = dict(self.telemetry.get(partner) or {})
        lm = self._level_modes(partner)
        moving = bool(tel.get("running")) and (tel.get("fan") is not None or
                                                (lm is not None and tel.get("fan_mode") in lm["external"]))
        ppol = store.get_policy(conn, partner) or {}
        ov = store.get_override(conn, partner, now)
        can_raise = ppol.get("enabled", True) and not (ov and ov[0] == "off")
        if moving or can_raise:
            return res
        return Resolution(False, dev_state.running, "safety",
                          f"{res.reason}; held: {partner} not moving air (off/intermittent, automation can't raise it)")

    def _level_modes(self, device_id):
        """A device whose `mode` trait carries an ordered `levels` list (the ERV: [low, med, high, turbo])
        is driven by threshold_ranged through its MODE: band level N -> levels[N-1], off -> `off_mode`.
        Returns {labels, vals, off_label, off_val, external} or None."""
        ctl = self.registry.get(device_id)
        mcfg = (getattr(ctl, "traits_cfg", {}) or {}).get("mode") if ctl else None
        if not mcfg or not mcfg.get("levels"):
            return None
        values = mcfg.get("values") or {}
        labels = [str(x) for x in mcfg["levels"] if str(x) in values]
        off_label = str(mcfg.get("off_mode", "off"))
        return {"labels": labels, "vals": [int(values[x]) for x in labels],
                "off_label": off_label if off_label in values else None,
                "off_val": int(values[off_label]) if off_label in values else None,
                "external": {int(k) for k in (mcfg.get("external") or {})}}

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
        # No setpoint in this status sample -> we cannot tell whether the park already landed, so we
        # cannot act without re-commanding blindly. Observed live: the Midea intermittently omits
        # `target`, and re-issuing on every such tick produced a stream of
        # "setpoint None -> 85.0 (park) status=mismatch" and pointless repeat commands to the appliance.
        # Wait for a sample we can actually compare against; the next tick is 45s away and the park/
        # restore is idempotent, so nothing is lost by skipping.
        if cur is None:
            return
        if want_park:
            if saved is None:
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
        if float(cur) == desired:
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
            st = {"running": tel.get("running"), "fan": tel.get("fan"), "fan_mode": tel.get("fan_mode"),
                  "lease_left_s": tel.get("lease_left_s")}
            transport = "wifi-mqtt"
            lm = self._level_modes(device_id)
            if lm is not None and st["fan_mode"] in lm["external"]:
                # the device entered a state on its own (ERV: OVR latch / power-on start-up) and ignores mode
                # writes until it ends — commanding into it just logs mismatches. Hold; the PWA shows why.
                store.append_log(conn, device_id, bool(st["running"]), "safety",
                                 f"device in external state {st['fan_mode']} -> hold", False, "hold")
                return

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
        res = self._apply_airflow_interlock(conn, device_id, pol, res, dev_state, now)
        res = self._apply_outdoor_gate(pol, res, dev_state, now)
        lm = self._level_modes(device_id) if drv is None else None
        if lm is not None:
            res = self._apply_ventilation_floor(conn, device_id, res, dev_state, override, now, lm)
        if lm is not None and res.running and res.level is None and res.source == "override" and lm["labels"]:
            # BOOST override on a level-mode device means its TOP level (ERV: turbo), not merely "on"
            top = len(lm["labels"])
            res = Resolution(True, dev_state.level != top, res.source, res.reason + f" -> level {top}",
                             level=top)
        reason = res.reason + (f" (via fallback {used_id})" if via_fallback and res.source == "rule" else "")
        if lm is not None:
            # a level-mode device's "speed N"/"level N" are its MODES — log what the operator sees in the PWA
            reason = _level_words(reason, lm["labels"])
        status = "noop"
        if res.act and not dry_run and lm is not None:
            # level-mode device (ERV): everything goes through its MODE — off -> off_mode, level N ->
            # levels[N-1]. No switchable trait exists; the ERV is never power-cut by automation.
            if not res.running:
                target = lm["off_label"]
            else:
                lvl = res.level if res.level is not None else 1
                target = lm["labels"][max(1, min(len(lm["labels"]), int(lvl))) - 1]
            if target is None:                         # no off_mode configured: cannot express OFF
                from types import SimpleNamespace
                result = SimpleNamespace(status="rejected")
            else:
                result = self.issuer.issue(device_id=device_id, trait="mode", action="set",
                                           args={"mode": target})
            status = result.status
            if result.status == "ok" and res.running != dev_state.running:
                store.record_transition(conn, device_id, res.running, now)
            self._emit(device_id, transport, ev.from_issue_status(result.status), res.reason)
        elif res.act and not dry_run:
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
        if (not res.act and not dry_run and res.running and dev_state.running
                and st.get("lease_left_s") is not None and st["lease_left_s"] < LEASE_RENEW_BELOW_S):
            # a leased call (the Aprilaire's DH) runs out on its own unless renewed — that IS its fail-safe
            r = self.issuer.issue(device_id=device_id, trait="switchable", action="set", args={"on": True})
            status = f"renewed:{r.status}"
        if pol.get("ventilation"):
            self._track_ventilation_demand(device_id, pol, bool(dev_state.running), res.running, now)
        # The other half of a graceful off: idle MODE alone leaves the appliance self-regulating to its
        # old target, so a manual override also parks the setpoint at its inert end (and restores it when
        # the override ends). Runs every tick, not just on a transition — see _apply_setpoint_park.
        self._apply_setpoint_park(conn, device_id, res, st, now, dry_run)
        store.append_log(conn, device_id, res.running, res.source, reason, res.act, status)
        log.info("%s -> %s | %s | act=%s status=%s%s", device_id,
                 (_level_words(f"speed {res.level}", lm["labels"]) if lm is not None and res.level is not None
                  else f"speed {res.level}" if res.level is not None else ("ON" if res.running else "OFF")),
                 reason, res.act, status,
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
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        apply_credentials(c)
        c.on_message = self.on_message

        # Subscribe to EVERY device's state, not to the set of source_sensors read at startup. That set
        # was computed once and never recomputed, which meant (a) fallback_sensors were never subscribed
        # at all, so a failover chain could not fail over, and (b) re-pointing a device's source from the
        # PWA did nothing until someone restarted ha-controller — the edit saved, the card kept reading
        # "stale", and nothing said why. on_message already keys the cache by the payload's device_id and
        # ignores anything a policy isn't asking for, so the wildcard costs one dict entry per device.
        def on_connect(cl, u, f, rc, props=None):
            cl.subscribe("home/+/+/state", qos=0)
            log.info("subscribed to home/+/+/state (all devices; sources resolve per-tick from policy)")
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
    ap.add_argument("--hot-db", default=os.environ.get("HA_HOT_DB", "instance/db/hot.db"),
                    help="readings store, read-only — resolves DERIVED_METRICS (air_quality)")
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
    erv_ids = [d for d, c in registry.items() if getattr(c, "device_type", None) == "erv"]
    for erv_id in erv_ids:
        store.seed_policy(conn, erv_id, ERV_POLICY)     # disabled until the operator picks sensors
    for dh_id in (d for d, c in registry.items() if getattr(c, "device_type", None) == "dehum"):
        store.seed_policy(conn, dh_id, {**DEHUM_POLICY,
                                        "ventilation": {**DEHUM_POLICY["ventilation"],
                                                        "device": erv_ids[0] if erv_ids else None}})

    conn.close()
    ctrl = Controller(issuer, drivers, registry, a.db, hot_db=a.hot_db)
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
        c.on_connect = lambda cl, u, f, rc, props=None: cl.subscribe("home/+/+/state")
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
