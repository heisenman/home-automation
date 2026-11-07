"""Thermostat comfort range set from the PWA (Hugh, 2026-11-07): the registry's `comfort` is the default, a
house setting overrides it, and the comfort warning follows the selected range."""
import sqlite3

from server.api.viewmodel import build_controls, comfort_alert
from server.control import comfort as CF
from server.control import control_store as store
from server.control.issuer import DeviceCtl

TR = {"mode": {"values": {"off": 0, "heat": 1, "cool": 2, "auto": 3}},
      "setpoint": {"unit": "degF", "channels": {
          "heat": {"min": 50, "max": 80, "comfort": {"min": 62, "max": 74}},
          "cool": {"min": 65, "max": 90, "comfort": {"min": 72, "max": 82}}}}}
REG = {"thermostat_hall": DeviceCtl("thermostat_hall", "venstar", "hall", TR, device_type="thermostat"),
       "lamp": DeviceCtl("lamp", "x", "y", {"switchable": {}})}


def _conn():
    c = sqlite3.connect(":memory:")
    store.ensure_schema(c)
    return c


def test_default_warns_at_75_and_a_selected_range_clears_it():
    c = _conn()
    act = {"mode": 1, "heat_setpoint": 75}
    assert comfort_alert(CF.with_overrides(c, TR, "thermostat_hall"), act)["kind"] == "comfort_limits"
    assert CF.handle_set(c, REG, "thermostat_hall", {"channel": "heat", "min": 62, "max": 76})[0] == 200
    tr = CF.with_overrides(c, TR, "thermostat_hall")
    assert tr["setpoint"]["channels"]["heat"]["comfort"] == {"min": 62.0, "max": 76.0}
    assert TR["setpoint"]["channels"]["heat"]["comfort"] == {"min": 62, "max": 74}       # registry untouched
    assert comfort_alert(tr, act) is None
    assert CF.handle_set(c, REG, "thermostat_hall", {"channel": "heat", "reset": True})[0] == 200
    assert comfort_alert(CF.with_overrides(c, TR, "thermostat_hall"), act) is not None


def test_validation():
    c = _conn()
    assert CF.handle_set(c, REG, "thermostat_hall", {"channel": "heat", "min": 76, "max": 70})[0] == 400
    assert CF.handle_set(c, REG, "thermostat_hall", {"channel": "heat", "min": 45, "max": 70})[0] == 400  # < manual
    assert CF.handle_set(c, REG, "thermostat_hall", {"channel": "fan", "min": 1, "max": 2})[0] == 400
    assert CF.handle_set(c, REG, "thermostat_hall", {"channel": "heat", "min": "x", "max": 2})[0] == 400
    assert CF.handle_set(c, REG, "lamp", {"channel": "heat", "min": 60, "max": 70})[0] == 404


def test_controls_carry_the_range_and_its_action():
    sp = next(x for x in build_controls(TR, manual=True) if x["kind"] == "setpoint")
    assert sp["comfort_action"]["path"] == "/devices/{id}/comfort"
    assert {ch["key"]: ch["comfort"] for ch in sp["channels"]}["heat"] == {"min": 62, "max": 74}
