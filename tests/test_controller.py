"""Tests for the ha-controller tick orchestration (server/control/controller.py)."""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.control import controller as C, control_store as store  # noqa: E402
from server.control.issuer import Result  # noqa: E402
from server.control.midea_driver import MideaDriver  # noqa: E402
from tests._harness import run_module  # noqa: E402

NOW = 1_000_000.0
STATUS_ON = "  running = True\n  humid%  = 30\n  target% = 35\n  fan = 40\n  tank = False\n  error = 0\n"
STATUS_ON_TANK = STATUS_ON.replace("tank = False", "tank = True")
# graceful-mode device: powered + in an operating mode (2=Continuous active, 1=Set idle)
STATUS_MODE_CONT = STATUS_ON + "  mode = 2\n"
STATUS_MODE_SET = STATUS_ON + "  mode = 1\n"
MODE_TRAITS_CFG = {"mode": {"values": {"set": 1, "continuous": 2, "dry": 4},
                            "safe": "set", "run_mode": "continuous", "idle_mode": "set"}}


class FakeIssuer:
    def __init__(self):
        self.calls = []

    def issue(self, *, device_id, trait, action, args, **kw):
        self.calls.append({"device_id": device_id, "trait": trait, "args": args})
        return Result("ok", "ok", intended=args, reported=args)


class _Ctl:
    area = "living_room"


def _make(tmp, status_text):
    db = os.path.join(tmp, "control.db")
    conn = sqlite3.connect(db)
    store.ensure_schema(conn)
    store.seed_policy(conn, "dehumidifier_office", C.DEFAULT_POLICY)
    conn.close()
    drv = MideaDriver("ip", "t", "k", runner=lambda argv: status_text)
    iss = FakeIssuer()
    ctrl = C.Controller(iss, {"dehumidifier_office": drv}, {"dehumidifier_office": _Ctl()}, db)
    return ctrl, iss, db


class _CtlMode:
    area = "living_room"
    traits_cfg = MODE_TRAITS_CFG


def _make_mode(tmp, status_text):
    db = os.path.join(tmp, "control.db")
    conn = sqlite3.connect(db)
    store.ensure_schema(conn)
    store.seed_policy(conn, "dehumidifier_office", C.DEFAULT_POLICY)
    conn.close()
    drv = MideaDriver("ip", "t", "k", runner=lambda argv: status_text)
    iss = FakeIssuer()
    ctrl = C.Controller(iss, {"dehumidifier_office": drv}, {"dehumidifier_office": _CtlMode()}, db)
    return ctrl, iss, db


def test_graceful_off_switches_to_set_mode():
    """The key behaviour: when the rule wants OFF, a mode-capable dehumidifier is switched to Set mode
    (compressor idles at its target, spins down gracefully) — NEVER a hard power-off."""
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make_mode(tmp, STATUS_MODE_CONT)  # currently Continuous (actively dehumidifying)
        ctrl.inject_reading("meter_pro_living_room", 38.0, ts=NOW - 30)   # <40 -> rule wants OFF
        ctrl.tick(now=NOW)
        assert iss.calls[-1]["trait"] == "mode" and iss.calls[-1]["args"] == {"mode": "set"}
        assert all(c["args"] != {"on": False} for c in iss.calls)        # no hard power-off ever issued


def test_graceful_on_switches_to_continuous_mode():
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make_mode(tmp, STATUS_MODE_SET)   # currently Set (idle)
        ctrl.inject_reading("meter_pro_living_room", 60.0, ts=NOW - 30)   # >=44 -> rule wants ON
        ctrl.tick(now=NOW)
        assert iss.calls[-1]["trait"] == "mode" and iss.calls[-1]["args"] == {"mode": "continuous"}


def test_graceful_mode_deadband_holds_no_command():
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make_mode(tmp, STATUS_MODE_CONT)  # Continuous; RH 42 in deadband -> hold
        ctrl.inject_reading("meter_pro_living_room", 42.0, ts=NOW - 30)
        ctrl.tick(now=NOW)
        assert iss.calls == []                              # no mode churn in the deadband


def test_graceful_mode_publishes_current_mode():
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make_mode(tmp, STATUS_MODE_CONT)
        ctrl.mqtt = _FakeMqtt()
        ctrl.inject_reading("meter_pro_living_room", 50.0, ts=NOW - 30)
        ctrl.tick(now=NOW)
        st = [p for t, p in ctrl.mqtt.published if t.endswith("/state")][-1]
        assert st["metrics"].get("mode") == 2               # operating mode reaches the UI via telemetry


def test_turns_off_when_room_below_threshold():
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make(tmp, STATUS_ON)              # device currently ON
        ctrl.inject_reading("meter_pro_living_room", 38.0, ts=NOW - 30)   # <40 -> rule wants OFF
        ctrl.tick(now=NOW)
        assert iss.calls and iss.calls[-1]["args"] == {"on": False}
        rows = store.recent_log(sqlite3.connect(db), "dehumidifier_office")
        assert rows[0]["source"] == "rule" and rows[0]["acted"] == 1 and rows[0]["status"] == "ok"


def test_dry_run_decides_but_does_not_issue():
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make(tmp, STATUS_ON)
        ctrl.inject_reading("meter_pro_living_room", 38.0, ts=NOW - 30)
        ctrl.tick(now=NOW, dry_run=True)
        assert iss.calls == []                              # nothing issued
        rows = store.recent_log(sqlite3.connect(db), "dehumidifier_office")
        assert rows[0]["acted"] == 1 and rows[0]["status"] == "dry-run"


def test_interlock_tank_full_forces_off():
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make(tmp, STATUS_ON_TANK)         # device ON but tank full
        ctrl.inject_reading("meter_pro_living_room", 60.0, ts=NOW - 30)   # would want ON
        ctrl.tick(now=NOW)
        assert iss.calls[-1]["args"] == {"on": False}
        rows = store.recent_log(sqlite3.connect(db), "dehumidifier_office")
        assert rows[0]["source"] == "safety"


class _FakeMqtt:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0):
        import json as _json
        self.published.append((topic, _json.loads(payload)))


def test_published_state_carries_timestamp():
    """Regression: onboard self-reports must carry a fresh ts, else the writer's (device_id,ts,metric)
    unique index collapses them and INSERT OR IGNORE freezes onboard RH forever."""
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make(tmp, STATUS_ON)
        ctrl.mqtt = _FakeMqtt()
        ctrl.inject_reading("meter_pro_living_room", 50.0, ts=NOW - 30)
        ctrl.tick(now=NOW)
        states = [p for t, p in ctrl.mqtt.published if t.endswith("/state")]
        assert states, "no state published"
        st = states[-1]
        assert st.get("ts"), f"state missing ts: {st}"
        assert st["metrics"].get("humidity_pct") == 30      # onboard value present...
        assert st["meta"]["authoritative"] is False          # ...flagged non-authoritative


def test_published_state_carries_area():
    """Regression: the writer reads area from the PAYLOAD (writer.py: payload.get('area','unknown')),
    not the topic — so a missing area field stamps device_last_seen.area='unknown' and the device drops
    off the map. The self-report payload must include the registry area."""
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make(tmp, STATUS_ON)
        ctrl.mqtt = _FakeMqtt()
        ctrl.inject_reading("meter_pro_living_room", 50.0, ts=NOW - 30)
        ctrl.tick(now=NOW)
        topic, st = next((t, p) for t, p in ctrl.mqtt.published if t.endswith("/state"))
        assert st.get("area") == "living_room", f"state missing area: {st}"
        assert topic == "home/living_room/dehumidifier_office/state"   # topic + payload agree


class _CtlAt:
    def __init__(self, area):
        self.area = area


class _FakeReloader:
    """Stands in for RegistryReloader — .current() returns whatever the (simulated) control.yaml now holds."""
    def __init__(self, registry):
        self._registry = registry

    def current(self):
        return self._registry


def test_actuator_relocate_reloads_area_without_restart():
    """controller-area-reload: relocating an actuator edits its area in control.yaml. tick() must pick up
    the new area live (via the attached reloader) so the self-report stamps the NEW area — no restart, no
    pop-back. The controller is the 6th area-stamper, sibling of the ingest bridges' devices.yaml reload."""
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make(tmp, STATUS_ON)
        ctrl.mqtt = _FakeMqtt()
        ctrl.inject_reading("meter_pro_living_room", 50.0, ts=NOW - 30)
        # simulate a relocate: control.yaml now says h_office for this actuator
        ctrl.attach_registry_reloader(_FakeReloader({"dehumidifier_office": _CtlAt("h_office")}))
        ctrl.tick(now=NOW)
        topic, st = next((t, p) for t, p in ctrl.mqtt.published if t.endswith("/state"))
        assert st.get("area") == "h_office", f"reloaded area not applied: {st}"
        assert topic == "home/h_office/dehumidifier_office/state"


def test_fallback_source_used_when_primary_stale():
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make(tmp, STATUS_ON)                  # device ON
        conn = sqlite3.connect(db)
        pol = store.get_policy(conn, "dehumidifier_office")
        pol["fallback_sensors"] = ["meter_backup"]
        store.set_policy(conn, "dehumidifier_office", pol)
        conn.close()
        ctrl.inject_reading("meter_pro_living_room", 60.0, ts=NOW - 99999)  # primary STALE
        ctrl.inject_reading("meter_backup", 60.0, ts=NOW - 30)             # fallback FRESH, 60 -> ON
        ctrl.tick(now=NOW)
        # fallback fresh 60 >= 44 -> ON, device already ON -> no command. (Without fallback the stale
        # primary would default-OFF and issue {on:False}.) So no issue proves the fallback was used.
        assert iss.calls == []
        rows = store.recent_log(sqlite3.connect(db), "dehumidifier_office")
        assert "via fallback meter_backup" in rows[0]["reason"]


def test_disabled_policy_is_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make(tmp, STATUS_ON)
        conn = sqlite3.connect(db)
        pol = store.get_policy(conn, "dehumidifier_office")
        pol["enabled"] = False
        store.set_policy(conn, "dehumidifier_office", pol)
        conn.close()
        ctrl.inject_reading("meter_pro_living_room", 60.0, ts=NOW - 30)
        ctrl.tick(now=NOW)
        assert iss.calls == []                              # disabled -> no automation


def test_sleep_scene_parks_a_running_device():
    # device ON + RH 60 (rule would keep it ON), but Sleep scene forces OFF
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make(tmp, STATUS_ON)
        conn = sqlite3.connect(db)
        pol = store.get_policy(conn, "dehumidifier_office")
        pol["scenes"] = {"Sleep": {"off": True}}
        store.set_policy(conn, "dehumidifier_office", pol)
        store.set_scene(conn, "Sleep")
        conn.close()
        ctrl.inject_reading("meter_pro_living_room", 60.0, ts=NOW - 30)
        ctrl.tick(now=NOW)
        assert iss.calls and iss.calls[-1]["args"] == {"on": False}
        rows = store.recent_log(sqlite3.connect(db), "dehumidifier_office")
        assert rows[0]["source"] == "scene", rows[0]


def test_away_scene_relaxes_thresholds():
    # RH 50: ON under Home (>=44), but Away relaxes to on_above 60 -> stays OFF (deadband/below)
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make(tmp, STATUS_ON.replace("running = True", "running = False"))
        conn = sqlite3.connect(db)
        pol = store.get_policy(conn, "dehumidifier_office")
        pol["scenes"] = {"Away": {"on_above": 60, "off_below": 55}}
        store.set_policy(conn, "dehumidifier_office", pol)
        store.set_scene(conn, "Away")
        conn.close()
        ctrl.inject_reading("meter_pro_living_room", 50.0, ts=NOW - 30)
        ctrl.tick(now=NOW)
        assert iss.calls == []                              # relaxed -> no turn-on at RH 50


# ── setpoint park (the other half of a graceful off) ─────────────────────────────
# A mode-only "off" leaves the appliance self-regulating to whatever target it was left at, so a manual
# override also parks the setpoint at its INERT end. Which end that is, is config: `max` for a
# dehumidifier (nothing left to remove), `min` for a humidifier/heater.
PARK_MAX_CFG = {**MODE_TRAITS_CFG, "setpoint": {"min": 35, "max": 85, "park": "max"}}
PARK_MIN_CFG = {**MODE_TRAITS_CFG, "setpoint": {"min": 35, "max": 85, "park": "min"}}
NO_PARK_CFG = {**MODE_TRAITS_CFG, "setpoint": {"min": 35, "max": 85}}
STATUS_PARKED = STATUS_MODE_SET.replace("target% = 35", "target% = 85")


def _make_park(tmp, status_text, traits_cfg, override=None):
    db = os.path.join(tmp, "control.db")
    conn = sqlite3.connect(db)
    store.ensure_schema(conn)
    store.seed_policy(conn, "dehumidifier_office", C.DEFAULT_POLICY)
    if override is not None:
        store.set_override(conn, "dehumidifier_office", override, NOW + 3600)
    conn.close()
    ctl = type("_C", (), {"area": "living_room", "traits_cfg": traits_cfg})()
    drv = MideaDriver("ip", "t", "k", runner=lambda argv: status_text)
    iss = FakeIssuer()
    ctrl = C.Controller(iss, {"dehumidifier_office": drv}, {"dehumidifier_office": ctl}, db)
    return ctrl, iss, db


def _setpoints(iss):
    return [c["args"]["value"] for c in iss.calls if c["trait"] == "setpoint"]


def test_override_off_parks_setpoint_at_max():
    """The bug this closes: an override 'off' used to only send mode=Set, leaving the Midea chasing its
    old 35% target — it kept running right through the "pause" (found live 2026-08-02)."""
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make_park(tmp, STATUS_MODE_CONT, PARK_MAX_CFG, override="off")
        ctrl.inject_reading("meter_pro_living_room", 60.0, ts=NOW - 30)   # rule would want ON
        ctrl.tick(now=NOW)
        assert {"mode": "set"} in [c["args"] for c in iss.calls]          # still a graceful mode change
        assert _setpoints(iss) == [85.0]                                  # ...AND parked at the inert end
        assert store.get_park(sqlite3.connect(db), "dehumidifier_office") == 35.0   # original remembered


def test_park_direction_is_config_not_code():
    """A humidifier/heater is inert at the BOTTOM of its range — same code, `park: min`."""
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make_park(tmp, STATUS_PARKED, PARK_MIN_CFG, override="off")
        ctrl.inject_reading("meter_pro_living_room", 60.0, ts=NOW - 30)
        ctrl.tick(now=NOW)
        assert _setpoints(iss) == [35.0]
        assert store.get_park(sqlite3.connect(db), "dehumidifier_office") == 85.0


def test_expired_override_restores_the_saved_setpoint():
    with tempfile.TemporaryDirectory() as tmp:
        # parked at 85, override already gone (expired/cleared), 35 remembered as the original
        ctrl, iss, db = _make_park(tmp, STATUS_PARKED, PARK_MAX_CFG)
        conn = sqlite3.connect(db)
        store.set_park(conn, "dehumidifier_office", 35.0, NOW - 600)
        conn.close()
        ctrl.inject_reading("meter_pro_living_room", 60.0, ts=NOW - 30)
        ctrl.tick(now=NOW)
        assert _setpoints(iss) == [35.0]                                  # target handed back
        assert store.get_park(sqlite3.connect(db), "dehumidifier_office") is None   # park row dropped


def test_reparking_never_overwrites_the_original():
    """Tick 2 of a pause must not save the PARKED value as the thing to restore — else the original is
    lost and the device is stranded at 85% forever."""
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make_park(tmp, STATUS_MODE_CONT, PARK_MAX_CFG, override="off")
        ctrl.inject_reading("meter_pro_living_room", 60.0, ts=NOW - 30)
        ctrl.tick(now=NOW)
        ctrl.tick(now=NOW + 60)
        assert store.get_park(sqlite3.connect(db), "dehumidifier_office") == 35.0


def test_rule_driven_off_does_not_park():
    """Parking is for a HUMAN pause. Ordinary rule cycling keeps the plain graceful idle_mode so the
    device's own setpoint behaviour is untouched."""
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make_park(tmp, STATUS_MODE_CONT, PARK_MAX_CFG)
        ctrl.inject_reading("meter_pro_living_room", 38.0, ts=NOW - 30)   # <40 -> rule wants OFF
        ctrl.tick(now=NOW)
        assert {"mode": "set"} in [c["args"] for c in iss.calls]
        assert _setpoints(iss) == []
        assert store.get_park(sqlite3.connect(db), "dehumidifier_office") is None


def test_device_without_park_config_is_unchanged():
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make_park(tmp, STATUS_MODE_CONT, NO_PARK_CFG, override="off")
        ctrl.inject_reading("meter_pro_living_room", 60.0, ts=NOW - 30)
        ctrl.tick(now=NOW)
        assert _setpoints(iss) == []
        assert store.get_park(sqlite3.connect(db), "dehumidifier_office") is None


def test_status_without_a_setpoint_does_not_re_command_blindly():
    """Observed live: the Midea intermittently omits `target` from a status sample. With no value to
    compare, the park cannot tell whether it already landed — re-issuing produced a stream of
    'setpoint None -> 85.0 (park) status=mismatch' and pointless repeat commands to the appliance."""
    with tempfile.TemporaryDirectory() as tmp:
        no_target = STATUS_MODE_CONT.replace("  target% = 35\n", "")
        ctrl, iss, db = _make_park(tmp, no_target, PARK_MAX_CFG, override="off")
        conn = sqlite3.connect(db)
        store.set_park(conn, "dehumidifier_office", 35.0, NOW - 60)   # a park already in flight
        conn.close()
        ctrl.inject_reading("meter_pro_living_room", 60.0, ts=NOW - 30)
        ctrl.tick(now=NOW)
        ctrl.tick(now=NOW + 60)
        assert _setpoints(iss) == []                                   # waits for a comparable sample
        assert store.get_park(sqlite3.connect(db), "dehumidifier_office") == 35.0   # original intact


def test_park_is_dry_run_safe():
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make_park(tmp, STATUS_MODE_CONT, PARK_MAX_CFG, override="off")
        ctrl.inject_reading("meter_pro_living_room", 60.0, ts=NOW - 30)
        ctrl.tick(now=NOW, dry_run=True)
        assert iss.calls == []
        assert store.get_park(sqlite3.connect(db), "dehumidifier_office") is None


# ── derived control metrics (air_quality) ───────────────────────────────────────────
# air_quality is computed server-side and NEVER published on MQTT, so it can only reach the control loop
# through the stored series. These lock that path: without it, binding a purifier to a gas node saves
# fine and then silently never actuates, because "no reading" looks exactly like "sensor offline".
HOT_DDL = ("CREATE TABLE readings (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
           "device_id TEXT NOT NULL, device_type TEXT NOT NULL, area TEXT NOT NULL, "
           "transport TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL, unit TEXT NOT NULL, "
           "schema_v INTEGER NOT NULL DEFAULT 1, authoritative INTEGER NOT NULL DEFAULT 1)")


def _hot_db(tmp, rows):
    """rows = [(device_id, metric, value, epoch_ts)] -> a hot.db written with ISO-UTC timestamps."""
    import time as _t
    path = os.path.join(tmp, "hot.db")
    conn = sqlite3.connect(path)
    conn.execute(HOT_DDL)
    for did, metric, value, ts in rows:
        conn.execute("INSERT INTO readings (ts, device_id, device_type, area, transport, metric, value, "
                     "unit, schema_v, authoritative) VALUES (?,?,?,?,?,?,?,?,1,1)",
                     (_t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(ts)), did, "gas", "kitchen",
                      "derived", metric, float(value), ""))
    conn.commit()
    conn.close()
    return path


AQ_POLICY = {"enabled": True, "source_sensor": "gas_kitchen", "sensor_stale_min": 15,
             "control": {"strategy": "threshold_ranged", "metric": "air_quality",
                         "bands": [{"max": 20, "level": 4}, {"max": 40, "level": 3},
                                   {"max": 60, "level": 2}, {"max": None, "level": 1}]}}


def test_derived_metric_resolves_from_stored_series():
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, _, _ = _make(tmp, STATUS_ON)
        ctrl.hot_db = _hot_db(tmp, [("gas_kitchen", "air_quality", 34.5, NOW - 60)])
        r, used, via_fb = ctrl._pick_source(AQ_POLICY, 900.0, NOW)
        assert used == "gas_kitchen" and via_fb is False
        assert r.value == 34.5 and r.ts == NOW - 60      # the STORED ts, not "now"


def test_derived_metric_goes_stale_when_the_sampler_stops():
    """A frozen sampler must make the source go stale (fail safe), not present an old point as live."""
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, _, _ = _make(tmp, STATUS_ON)
        ctrl.hot_db = _hot_db(tmp, [("gas_kitchen", "air_quality", 34.5, NOW - 1800)])
        r, used, _ = ctrl._pick_source(AQ_POLICY, 900.0, NOW)   # 30 min old vs 15 min tolerance
        assert used == "gas_kitchen" and r.ts == NOW - 1800     # returned, but the caller sees it stale
        assert (NOW - r.ts) > 900.0


def test_derived_metric_absent_when_no_hot_db_configured():
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, _, _ = _make(tmp, STATUS_ON)
        ctrl.hot_db = None
        assert ctrl._pick_source(AQ_POLICY, 900.0, NOW) == (None, None, False)


def test_derived_lookup_ignores_ancient_points():
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, _, _ = _make(tmp, STATUS_ON)
        ctrl.hot_db = _hot_db(tmp, [("gas_kitchen", "air_quality", 34.5, NOW - 7200)])
        assert ctrl._pick_source(AQ_POLICY, 900.0, NOW)[0] is None      # beyond _DERIVED_MAX_AGE_S


def test_derived_lookup_survives_a_missing_hot_db():
    """A missing/locked readings store must never take down a tick — it degrades to 'no reading'."""
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, _, _ = _make(tmp, STATUS_ON)
        ctrl.hot_db = os.path.join(tmp, "does-not-exist.db")
        assert ctrl._pick_source(AQ_POLICY, 900.0, NOW) == (None, None, False)


def test_derived_metric_falls_back_to_a_second_gas_node():
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, _, _ = _make(tmp, STATUS_ON)
        ctrl.hot_db = _hot_db(tmp, [("gas_kitchen", "air_quality", 90.0, NOW - 1800),   # STALE
                                    ("gas_c_office", "air_quality", 30.0, NOW - 30)])   # FRESH
        pol = {**AQ_POLICY, "fallback_sensors": ["gas_c_office"]}
        r, used, via_fb = ctrl._pick_source(pol, 900.0, NOW)
        assert (used, via_fb, r.value) == ("gas_c_office", True, 30.0)


def test_non_derived_metric_never_reads_the_stored_series():
    """RH still comes from MQTT only — the hot.db path must not become a back door for live metrics."""
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, _, _ = _make(tmp, STATUS_ON)
        ctrl.hot_db = _hot_db(tmp, [("meter_pro_living_room", "humidity_pct", 60.0, NOW - 30)])
        assert ctrl._pick_source(C.DEFAULT_POLICY, 900.0, NOW) == (None, None, False)


if __name__ == "__main__":
    run_module(globals())
