"""Tests for the whole-house mode controller (server/control/mode.py)."""
from server.control.mode import ModeController
from tests._harness import raises, run_module

T0 = 1_780_000_000.0


def test_manual_set_requires_authorization():
    m = ModeController()
    assert m.set("Conserve", authorized=False) is False
    assert m.get() == "Normal"
    assert m.set("Conserve", authorized=True, now=T0) is True
    assert m.get() == "Conserve"


def test_manual_set_bad_mode_raises():
    with raises(ValueError):
        ModeController().set("Bogus", authorized=True)


def test_auto_enters_conserve_on_battery():
    m = ModeController()
    assert m.tick({"mains_present": False, "ups_pct": 90}, now=T0) == "Conserve"


def test_auto_emergency_on_critical_ups_bypasses_dwell():
    m = ModeController(hysteresis_s=300)
    m.tick({"mains_present": False, "ups_pct": 90}, now=T0)          # -> Conserve
    # escalation to Emergency must NOT wait for dwell
    assert m.tick({"mains_present": False, "ups_pct": 10}, now=T0 + 5) == "Emergency"


def test_deadband_prevents_flapping():
    m = ModeController()
    m.tick({"mains_present": False, "ups_pct": 30}, now=T0)          # -> Conserve
    # mains back but ups only 50 (between enter=40 and leave=60) → stays Conserve
    assert m.tick({"mains_present": True, "ups_pct": 50}, now=T0 + 1000) == "Conserve"
    # ups now above leave bound → Normal
    assert m.tick({"mains_present": True, "ups_pct": 65}, now=T0 + 2000) == "Normal"


def test_de_escalation_waits_for_dwell():
    m = ModeController(hysteresis_s=300)
    m.tick({"mains_present": False, "ups_pct": 30}, now=T0)          # -> Conserve at T0
    # conditions clear quickly, but dwell not elapsed → hold Conserve
    assert m.tick({"mains_present": True, "ups_pct": 80}, now=T0 + 100) == "Conserve"
    # after dwell → Normal
    assert m.tick({"mains_present": True, "ups_pct": 80}, now=T0 + 400) == "Normal"


if __name__ == "__main__":
    run_module(globals())
