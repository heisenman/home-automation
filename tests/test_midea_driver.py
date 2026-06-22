"""Tests for the Midea driver + issuer transport (server/control/midea_driver.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.control import midea_driver as M  # noqa: E402
from server.control import protocol  # noqa: E402
from tests._harness import run_module  # noqa: E402

# a realistic CLI status block (as midea-beautiful-air-cli prints)
SAMPLE = """id 000.../150633095264247
  id      = 150633095264247
  addr    = 192.168.0.211
  model   = Dehumidifier
  online  = True
  running = True
  humid%  = 30
  target% = 35
  temp    = 23.0
  fan     = 40
  mode    = 2
  tank    = False
  error   = 0
"""


def test_parse_status_typed():
    s = M._parse_status(SAMPLE)
    assert s["running"] is True and s["online"] is True and s["tank_full"] is False
    assert s["target"] == 35 and s["humidity"] == 30 and s["fan"] == 40 and s["mode"] == 2
    assert s["temp"] == 23.0 and s["error"] == 0


def test_driver_set_builds_argv():
    seen = {}

    def runner(argv):
        seen["argv"] = argv
        return SAMPLE
    drv = M.MideaDriver("192.168.0.211", "TOK", "KEY", runner=runner)
    drv.set(running=False)
    a = seen["argv"]
    assert a[1] == "set" and "--running" in a and a[a.index("--running") + 1] == "False"
    assert "--ip" in a and "--token" in a and "--key" in a


def test_transport_switchable_maps_to_running():
    captured = {}

    def runner(argv):
        captured["argv"] = argv
        return SAMPLE.replace("running = True", "running = False")
    drv = M.MideaDriver("ip", "t", "k", runner=runner)
    tr = M.MideaTransport({"dehumidifier_office": drv})
    cmd = {"id": "c1", "trait": "switchable", "action": "set", "args": {"on": False}}
    ack = tr.send_and_wait(node="server", device_id="dehumidifier_office", area="c_office", cmd=cmd)
    assert ack["status"] == "ok" and ack["reported_state"] == {"on": False}
    assert "--running" in captured["argv"]


def test_transport_setpoint_maps_to_target():
    drv = M.MideaDriver("ip", "t", "k", runner=lambda argv: SAMPLE.replace("target% = 35", "target% = 45"))
    tr = M.MideaTransport({"dehumidifier_office": drv})
    cmd = {"id": "c2", "trait": "setpoint", "action": "set", "args": {"value": 45}}
    ack = tr.send_and_wait(node="server", device_id="dehumidifier_office", area="c_office", cmd=cmd)
    assert ack["status"] == "ok" and ack["reported_state"] == {"value": 45}


def test_transport_unknown_device_returns_none():
    tr = M.MideaTransport({})
    ack = tr.send_and_wait(node="server", device_id="nope", area="x",
                           cmd={"id": "c", "trait": "switchable", "action": "set", "args": {"on": True}})
    assert ack is None


def test_transport_unsupported_trait_rejected():
    drv = M.MideaDriver("ip", "t", "k", runner=lambda argv: SAMPLE)
    tr = M.MideaTransport({"d": drv})
    ack = tr.send_and_wait(node="server", device_id="d", area="x",
                           cmd={"id": "c", "trait": "lockable", "action": "lock", "args": {}})
    assert ack["status"] == "rejected"


def test_routing_transport_dispatches_by_device():
    from server.control.issuer import RoutingTransport
    seen = {"default": 0}

    class FakeDefault:
        def send_and_wait(self, **kw):
            seen["default"] += 1
            return protocol.build_ack(cmd_id=kw["cmd"]["id"], status="ok")
    drv = M.MideaDriver("ip", "t", "k", runner=lambda argv: SAMPLE)
    rt = RoutingTransport(FakeDefault(), {"dehumidifier_office": M.MideaTransport({"dehumidifier_office": drv})})
    # Midea device -> Midea transport (default not touched)
    a1 = rt.send_and_wait(node="server", device_id="dehumidifier_office", area="living_room",
                          cmd={"id": "x", "trait": "switchable", "action": "set", "args": {"on": True}})
    assert a1["status"] == "ok" and seen["default"] == 0
    # anything else -> default (MQTT path)
    rt.send_and_wait(node="c6-bench", device_id="meter_x", area="kitchen",
                     cmd={"id": "y", "trait": "switchable", "action": "set", "args": {"on": True}})
    assert seen["default"] == 1


def test_load_drivers_from_env():
    d = M.load_drivers_from_env({"MIDEA_IP": "1.2.3.4", "MIDEA_TOKEN": "t", "MIDEA_KEY": "k"},
                                "dehumidifier_office")
    assert "dehumidifier_office" in d and d["dehumidifier_office"].ip == "1.2.3.4"
    assert M.load_drivers_from_env({"MIDEA_IP": "1.2.3.4"}, "x") == {}   # missing token/key


if __name__ == "__main__":
    run_module(globals())
