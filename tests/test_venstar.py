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

    def log_message(self, *a):
        pass

    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
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
    Fake.info, Fake.posts = dict(INFO), []
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


def test_absent_humidity_is_not_reported_as_zero():
    assert "humidity_pct" not in info_metrics(INFO)[0]


if __name__ == "__main__":
    from tests._harness import run_module
    run_module(globals())
