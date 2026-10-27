"""Venstar ColorTouch driver + bridge (ADR-0043) against a FAKE thermostat that enforces the published spec's
rules (mode calls need both setpoints; auto respects setpointdelta; HTTP 200 + {"error":true} on refusal).
The real T7850 still has to be verified live (intake §5) — this proves our side of the contract."""
import json
import threading
import urllib.parse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

from server.control.issuer import CommandIssuer
from server.control.policy import PolicyStore
from server.control.registry import parse_control_registry
from server.control.traits import TraitError, get_trait
from server.control.venstar_api import (VenstarClient, VenstarError, info_metrics, plan_mode, plan_setpoint)
from server.control.venstar_driver import VenstarTransport, venstar_devices_of
from server.ingest.venstar_bridge import messages
from tests._harness import raises

INFO = {"name": "Hall", "mode": 1, "state": 0, "activestage": 0, "fan": 0, "fanstate": 0, "tempunits": 0,
        "schedule": 1, "schedulepart": 1, "away": 0, "spacetemp": 68, "heattemp": 68, "cooltemp": 76,
        "cooltempmin": 35, "cooltempmax": 99, "heattempmin": 35, "heattempmax": 99, "setpointdelta": 2,
        "hum": 0, "availablemodes": 0}

REG = parse_control_registry({"devices": {
    "thermostat_hall": {"node": "venstar", "area": "hall", "device_type": "thermostat", "manual": True,
                        "traits": {"mode": {"values": {"off": 0, "heat": 1, "cool": 2, "auto": 3}},
                                   "setpoint": {"unit": "degF", "channels": {"heat": {"min": 50, "max": 80},
                                                                             "cool": {"min": 65, "max": 90}}}}},
    "hvac_blower": {"node": "venstar", "area": "hall", "device_type": "hvac_fan", "manual": True,
                    "traits": {"switchable": {"label": "Furnace blower"}}}}})


class Fake(BaseHTTPRequestHandler):
    info: dict = {}
    posts: list = []
    basic: str | None = None                     # "user:pass" -> demand HTTP Basic auth (the T7850 manual's term)

    def log_message(self, *a):
        pass

    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _authed(self):
        import base64
        if Fake.basic is None or self.headers.get("Authorization") == "Basic " + base64.b64encode(
                Fake.basic.encode()).decode():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="venstar"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if not self._authed():
            return
        if self.path == "/query/info":
            return self._send(Fake.info)
        if self.path == "/":
            return self._send({"api_ver": 4, "type": "residential", "model": "T7850", "firmware": "6.0"})
        self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        p = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
        Fake.posts.append(p)
        i = Fake.info
        if "mode" in p and not ("heattemp" in p and "cooltemp" in p):
            return self._send({"error": True, "reason": "mode requires heattemp and cooltemp"})
        h, c = float(p.get("heattemp", i["heattemp"])), float(p.get("cooltemp", i["cooltemp"]))
        if c - h < i["setpointdelta"]:
            return self._send({"error": True, "reason": "setpointdelta"})
        i.update(heattemp=h, cooltemp=c)
        if "mode" in p:
            i["mode"] = int(p["mode"])
        if "fan" in p:
            i["fan"] = i["fanstate"] = int(p["fan"])
        return self._send({"success": True})


@contextmanager
def _fake():
    Fake.info, Fake.posts, Fake.basic = dict(INFO), [], None
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def _issuer(host):
    devs = venstar_devices_of(REG)
    t = VenstarTransport(devs, VenstarClient(host), {"thermostat_hall": "degF"}, readback_delay_s=0)
    return CommandIssuer(registry=REG, secrets={d: "x" * 32 for d in REG}, policy=PolicyStore({"version": 1}),
                         transport=t)


def test_blower_on_sends_fan_only_and_reads_it_back():
    with _fake() as fake:
        r = _issuer(fake).issue(device_id="hvac_blower", trait="switchable", action="set", args={"on": True})
        assert r.status == "ok" and r.reported == {"on": True}
        assert Fake.posts == [{"fan": "1"}]                      # fan alone: no mode, so no setpoints needed


def test_heat_setpoint_keeps_the_cool_one():
    with _fake() as fake:
        r = _issuer(fake).issue(device_id="thermostat_hall", trait="setpoint", action="set",
                                args={"channel": "heat", "value": 70})
        assert r.status == "ok" and r.reported == {"channel": "heat", "value": 70.0}
        assert Fake.posts == [{"heattemp": "70", "cooltemp": "76"}]


def test_mode_change_carries_both_setpoints():
    with _fake() as fake:
        r = _issuer(fake).issue(device_id="thermostat_hall", trait="mode", action="set", args={"mode": "cool"})
        assert r.status == "ok" and Fake.info["mode"] == 2
        assert Fake.posts == [{"mode": "2", "heattemp": "68", "cooltemp": "76"}]


def test_setpoint_that_would_crowd_the_other_is_refused_not_shifted():
    with _fake() as fake:
        r = _issuer(fake).issue(device_id="thermostat_hall", trait="setpoint", action="set",
                                args={"channel": "heat", "value": 75})
        assert r.status == "rejected" and "setpointdelta" in r.reason and Fake.posts == []


def test_manual_range_is_the_trait_bound():
    with _fake() as fake:
        r = _issuer(fake).issue(device_id="thermostat_hall", trait="setpoint", action="set",
                                args={"channel": "cool", "value": 95})
        assert r.status == "rejected" and Fake.posts == []


def test_unit_mismatch_is_refused():
    with raises(VenstarError):
        plan_setpoint({**INFO, "tempunits": 1}, "heat", 20, "degF")


def test_mode_outside_availablemodes_is_refused():
    with raises(VenstarError):
        plan_mode({**INFO, "availablemodes": 2}, 2)            # heat-only system: no cool


def test_unreachable_thermostat_is_no_ack_not_a_crash():
    r = _issuer("127.0.0.1:9").issue(device_id="hvac_blower", trait="switchable", action="set", args={"on": True})
    assert r.status == "no-ack"


def test_setpoint_trait_channels():
    sp = get_trait("setpoint")
    cfg = {"channels": {"heat": {"min": 50, "max": 80}}}
    assert sp.validate_command("set", {"channel": "heat", "value": 72}, cfg) == {"channel": "heat", "value": 72.0}
    with raises(TraitError):
        sp.validate_command("set", {"channel": "cool", "value": 72}, cfg)
    assert sp.validate_command("set", {"value": 20}, {"min": 10, "max": 30}) == {"value": 20.0}   # unchanged


def test_bridge_messages_in_celsius_and_self_report_flagged():
    msgs = dict(messages({**INFO, "fanstate": 1, "hum": 41}, {"thermostat_hall": ("thermostat", "hall"),
                                                              "hvac_blower": ("hvac_fan", "hall")}, "T"))
    t = msgs["home/hall/thermostat_hall/state"]
    assert t["metrics"]["temperature_c"] == 20.0 and t["metrics"]["cool_setpoint_c"] == 24.44
    assert t["metrics"]["humidity_pct"] == 41 and t["meta"]["authoritative"] is False
    b = msgs["home/hall/hvac_blower/state"]
    assert b["metrics"] == {"running": 1, "fan_on_setting": 0} and b["meta"]["authoritative"] is True


def test_basic_auth_login_works():
    with _fake() as fake:
        Fake.basic = "ha:pw"
        assert VenstarClient(fake, user="ha", password="pw").info()["mode"] == 1
        with raises(OSError):                                  # urllib HTTPError is an OSError
            VenstarClient(fake).info()


def test_absent_humidity_is_not_reported_as_zero():
    assert "humidity_pct" not in info_metrics(INFO)[0]


# ── follow coupling: the blower follows the attic dehumidifier (Hugh, 2026-10-26) ─────────────────────────
def test_follow_decide_edges_and_person_wins():
    from server.control.follow import decide, initial
    cmd, st, _ = decide(True, False, initial())                     # dehum starts, blower on auto
    assert cmd is True and st == {"leader_on": True, "engaged": True}
    assert decide(True, False, st)[0] is None                        # a person set Auto mid-run: not re-asserted
    assert decide(False, True, st)[0] is False                       # dehum stops: we hand it back to Auto
    assert decide(False, False, st) == (None, initial(), "leader stopped; follower already off")
    cmd, st, _ = decide(True, True, initial())                       # a person already had it On
    assert cmd is None and st["engaged"] is False
    assert decide(False, True, st)[0] is None                        # ...so it stays On after the dehum stops
    assert decide(None, False, initial())[0] is None                 # stale/missing telemetry: do nothing
    on = {"leader_on": True, "engaged": True}
    _, st, why = decide(True, False, on)                             # someone switched it off mid-run...
    assert st["engaged"] is False and "not ours" in why
    assert decide(False, True, st)[0] is None                        # ...then a person's On survives the stop
    assert decide(True, False, {**on, "at": 1000.0}, now=1060.0)[1]["engaged"] is True   # lagging poll, not a person


def test_follow_pwa_press_releases_the_follower():
    import sqlite3
    import tempfile
    from server.api.control import release_follow
    from server.control import control_store as store
    with tempfile.TemporaryDirectory() as d:
        db = f"{d}/control.db"
        c = sqlite3.connect(db)
        store.ensure_schema(c)
        store.set_setting(c, "follow:hvac_blower", {"leader_on": True, "engaged": True})
        c.close()
        release_follow(db, "hvac_blower")
        release_follow(db, "not_a_follower")                         # no follow state: untouched, no error
        c = sqlite3.connect(db)
        assert store.get_setting(c, "follow:hvac_blower") == {"leader_on": True, "engaged": False}
        assert store.get_setting(c, "follow:not_a_follower") is None


def _follow_ctrl(db):
    import time
    from server.control import controller as C
    from server.control.issuer import Result

    class Iss:
        calls = []

        def issue(self, *, device_id, trait, action, args, **kw):
            Iss.calls.append((device_id, trait, args))
            return Result("ok", "ok", intended=args, reported=args)

    reg = parse_control_registry({"devices": {
        "dehum_attic": {"node": "dehum_c6", "area": "attic", "device_type": "dehum",
                        "traits": {"switchable": {"safe_on": False}}},
        "hvac_blower": {"node": "venstar", "area": "mech_closet", "device_type": "hvac_fan", "manual": True,
                        "traits": {"switchable": {"now_metric": "fan_on_setting",
                                                  "follow": {"device": "dehum_attic"}}}}}})
    ctrl = C.Controller(Iss(), {}, reg, db)

    def feed(did, metrics):
        class M:
            payload = json.dumps({"device_id": did, "metrics": metrics}).encode()
        ctrl.on_message(None, None, M)
    return ctrl, Iss.calls, feed, time.time


def test_blower_follows_the_dehum_through_the_controller_and_survives_a_restart():
    import sqlite3
    import tempfile
    from server.control import control_store as store
    with tempfile.TemporaryDirectory() as d:
        db = f"{d}/control.db"
        ctrl, calls, feed, now = _follow_ctrl(db)
        feed("hvac_blower", {"running": 0, "fan_on_setting": 0})
        feed("dehum_attic", {"dh_call": 1, "call_left_s": 600})
        ctrl.tick(now=now())
        assert calls == [("hvac_blower", "switchable", {"on": True})]
        ctrl.tick(now=now())                                         # no edge -> no repeat; the blower still
        assert len(calls) == 1                                       # reads off (poll lag): stays ours
        ctrl2, calls2, feed2, _ = _follow_ctrl(db)                   # controller restarts mid-run
        calls2.clear()
        feed2("hvac_blower", {"running": 1, "fan_on_setting": 1})
        feed2("dehum_attic", {"dh_call": 0, "call_left_s": 0})
        ctrl2.tick(now=now())
        assert calls2 == [("hvac_blower", "switchable", {"on": False})]
        c = sqlite3.connect(db)
        assert store.get_setting(c, "follow:hvac_blower") == {"leader_on": False, "engaged": False}


# ── BFF: render-ready thermostat + blower controls, °F values, comfort warning ─────────────────────────────
TRAITS = {"mode": {"values": {"off": 0, "heat": 1, "cool": 2, "auto": 3}, "now_metric": "hvac_mode",
                   "activity": {"metric": "hvac_state", "labels": {0: "idle", 1: "heating", 2: "cooling"}}},
          "setpoint": {"unit": "degF", "channels": {
              "heat": {"min": 50, "max": 80, "now_metric": "heat_setpoint_c", "comfort": {"min": 62, "max": 74}},
              "cool": {"min": 65, "max": 90, "now_metric": "cool_setpoint_c", "comfort": {"min": 72, "max": 82}}}}}


def test_bff_thermostat_channels_and_blower_switch():
    from server.api.viewmodel import build_controls
    sp = [c for c in build_controls(TRAITS, manual=True) if c["kind"] == "setpoint"][0]
    assert [(c["key"], c["now_key"]) for c in sp["channels"]] == [("heat", "heat_setpoint"),
                                                                    ("cool", "cool_setpoint")]
    assert sp["unit"] == "°F" and sp["action"]["channel_key"] == "channel"
    sw = build_controls({"switchable": {"label": "Furnace blower", "on_label": "On", "off_label": "Auto"}},
                        manual=True)
    assert [(c["kind"], c["on_label"], c["off_label"]) for c in sw] == [("switchable", "On", "Auto")]
    assert build_controls({"switchable": {}}, manual=False)[0]["kind"] == "override"   # automated: unchanged


def test_bff_now_values_in_fahrenheit_and_state_label():
    from server.api.viewmodel import manual_state_label, trait_now_values
    db = {"hvac_mode": 1, "hvac_state": 1, "heat_setpoint_c": 21.67, "cool_setpoint_c": 26.11}
    act = trait_now_values(lambda m: (db[m], "T") if m in db else None, TRAITS)
    assert act == {"mode": 1, "heat_setpoint": 71.0, "cool_setpoint": 79.0, "activity": "heating", "active": True}
    assert manual_state_label(TRAITS, act) == "Heat · heating"


def test_comfort_warning_only_for_the_channel_the_mode_uses():
    from server.api.viewmodel import build_alerts, comfort_alert
    a = comfort_alert(TRAITS, {"mode": 1, "heat_setpoint": 76, "cool_setpoint": 79})
    assert a["level"] == "warn" and "Heat setpoint 76°F" in a["text"] and "62–74°F" in a["text"]
    assert comfort_alert(TRAITS, {"mode": 2, "heat_setpoint": 76, "cool_setpoint": 79}) is None   # cooling
    assert comfort_alert(TRAITS, {"mode": 0, "heat_setpoint": 90, "cool_setpoint": 95}) is None   # off
    assert comfort_alert(TRAITS, {"mode": 3, "heat_setpoint": 70, "cool_setpoint": 85})["text"].startswith("Cool")
    out = build_alerts([], [{"device_id": "thermostat_hall", "alert": a}], 0)
    assert [(x["severity"], x["kind"]) for x in out] == [("warning", "comfort_limits")]


if __name__ == "__main__":
    from tests._harness import run_module
    run_module(globals())
