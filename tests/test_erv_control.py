"""ERV as a PWA actuator (2026-10-04): signed edge transport, shared (ts, seq) counter, the `timed` trait,
and the manual-only display path. Values are the live-verified Broan codes (design §7.2 log)."""
import hashlib
import hmac
import json

import pytest

from server.api.viewmodel import build_controls
from server.control import erv_driver as E
from server.control import traits
from server.control.edge_cmd import next_stamp, signed_command
from server.control.registry import parse_control_registry

ERV_TRAITS = {"mode": {"values": {"off": 1, "int": 8, "low": 9, "med": 11, "high": 10, "turbo": 12},
                       "safe": "med"},
              "timed": {"max": 60, "presets_min": [15, 30, 60]}}


# ── shared (ts, seq) counter ──────────────────────────────────────────────────────────────────────

def test_stamp_is_monotonic_within_a_second_and_resets_on_a_new_one(tmp_path):
    assert next_stamp("n", tmp_path, now=1000) == (1000, 0)
    assert next_stamp("n", tmp_path, now=1000) == (1000, 1)
    assert next_stamp("n", tmp_path, now=1000.9) == (1000, 2)
    assert next_stamp("n", tmp_path, now=1001) == (1001, 0)


def test_stamp_never_moves_ts_backwards_or_forwards_of_the_wall_clock(tmp_path):
    next_stamp("n", tmp_path, now=2000)
    # a clock that stepped back: hold the last ts, bump seq — never re-use (2000, 0)
    assert next_stamp("n", tmp_path, now=1990) == (2000, 1)


def test_stamp_file_is_the_one_the_cli_uses(tmp_path):
    """tools/shade_cmd.py (and erv_cmd.py) persist `.<node>_cmd_seq` as "ts seq" — same file, same format,
    so a PWA command and a CLI command in the same second can't replay each other out."""
    next_stamp("hvac_c6", tmp_path, now=3000)
    assert (tmp_path / ".hvac_c6_cmd_seq").read_text() == "3000 0"


def test_signed_command_is_what_the_firmware_verifies(tmp_path):
    env = signed_command("hvac_c6", "k3y", {"op": "erv_mode", "mode": "med"}, tmp_path, now=4000)
    assert set(env) == {"p", "s"}
    assert env["s"] == hmac.new(b"k3y", env["p"].encode(), hashlib.sha256).hexdigest()
    assert json.loads(env["p"]) == {"op": "erv_mode", "mode": "med", "ts": 4000, "seq": 0}


# ── transport plan (no broker) ────────────────────────────────────────────────────────────────────

def _t():
    t = E.ErvEdgeTransport.__new__(E.ErvEdgeTransport)       # skip paho; _plan is pure
    return t


def test_mode_maps_broan_int_to_node_name_and_confirms_on_fan_mode():
    (inner, ok, rep), why = _t()._plan({"trait": "mode", "action": "set", "args": {"mode": 11}})
    assert why is None and inner == {"op": "erv_mode", "mode": "med"}
    assert ok({"fan_mode": 11}) and not ok({"fan_mode": 10})
    assert rep({"fan_mode": 11}) == {"mode": 11}


def test_every_configured_mode_has_a_node_name_and_override_does_not():
    assert set(ERV_TRAITS["mode"]["values"].values()) == set(E.ERV_MODE_NAMES)
    assert 2 not in E.ERV_MODE_NAMES                        # 0x02 = OVR: wedges the unit, never written


def test_boost_confirms_on_relay_state():
    (inner, ok, rep), _ = _t()._plan({"trait": "timed", "action": "set", "args": {"minutes": 20}})
    assert inner == {"op": "erv_boost", "min": 20}
    assert ok({"ovr_boost": True}) and not ok({"ovr_boost": False})
    assert rep({"ovr_boost": True})["minutes"] == 20
    (inner, ok, _), _ = _t()._plan({"trait": "timed", "action": "set", "args": {"minutes": 0}})
    assert inner == {"op": "erv_boost", "min": 0} and ok({"ovr_boost": False})


def test_unsupported_trait_is_rejected_not_sent():
    plan, why = _t()._plan({"trait": "switchable", "action": "set", "args": {"on": True}})
    assert plan is None and "unsupported" in why


# ── traits + registry ─────────────────────────────────────────────────────────────────────────────

def test_timed_trait_bounds_minutes_and_fails_safe_released():
    t = traits.get_trait("timed")
    assert t.validate_command("set", {"minutes": 30}, {"max": 60}) == {"minutes": 30}
    with pytest.raises(traits.TraitError):
        t.validate_command("set", {"minutes": 61}, {"max": 60})
    assert t.safe_state({}) == {"minutes": 0}


def test_erv_registry_entry_parses_as_manual_and_routes_by_type():
    reg = parse_control_registry({"devices": {"erv_attic": {
        "node": "hvac_c6", "area": "attic", "device_type": "erv", "manual": True, "traits": ERV_TRAITS}}})
    assert reg["erv_attic"].manual is True
    assert E.erv_devices_of(reg) == {"erv_attic": "hvac_c6"}
    assert traits.get_trait("mode").safe_state(reg["erv_attic"].traits_cfg["mode"]) == {"mode": 11}


# ── PWA controls ──────────────────────────────────────────────────────────────────────────────────

def test_manual_device_gets_no_dead_override_button():
    kinds = [c["kind"] for c in build_controls(ERV_TRAITS, manual=True)]
    assert kinds == ["mode", "timed"]
    assert "override" in [c["kind"] for c in build_controls(ERV_TRAITS)]   # policy devices unchanged


def test_timed_control_offers_presets_and_stop():
    c = next(c for c in build_controls(ERV_TRAITS, manual=True) if c["kind"] == "timed")
    assert [o["value"] for o in c["options"]] == [15, 30, 60, 0]
    assert c["action"]["arg_key"] == "minutes" and c["now_key"] == "boost_on"


# ── display path: a manual device has NO policy, which used to mean "not displayed at all" ──────────

def test_manual_device_without_policy_is_displayed_with_its_readback(tmp_path):
    import sqlite3
    from pathlib import Path

    from server.api import viewmodel as V
    from server.control import control_store as store
    from server.storage import writer as W

    cc = sqlite3.connect(":memory:")
    store.ensure_schema(cc)                                   # no policy seeded for erv_attic
    hc = W._open_db(Path(tmp_path) / "hot.db")
    W._insert_readings(hc, {"schema": 1, "device_id": "erv_attic", "device_type": "erv", "area": "attic",
                            "transport": "rs485", "ts": "2026-10-04T20:40:00Z",
                            "metrics": {"fan_mode": 11, "ovr_boost": True, "power_w": 61.5}})
    reg = parse_control_registry({"devices": {"erv_attic": {
        "node": "hvac_c6", "area": "attic", "device_type": "erv", "manual": True, "traits": ERV_TRAITS}}})
    vm = V.build_display(cc, hc, "erv_attic", 1_791_146_000, registry=reg)
    assert vm is not None and vm["manual"] is True and vm["health"] == "manual"
    assert vm["actuator"]["mode"] == 11 and vm["actuator"]["boost_on"] is True   # from fan_mode / ovr_boost
    assert [c["kind"] for c in vm["controls"]] == ["mode", "timed"]
    # …while an unflagged policy-less device is still hidden (host LEDs etc. must not suddenly appear)
    reg["erv_attic"].manual = False
    assert V.build_display(cc, hc, "erv_attic", 1_791_146_000, registry=reg) is None


def test_unquoted_yaml_off_is_refused_at_load():
    import yaml
    data = yaml.safe_load("devices:\n  e:\n    node: n\n    area: a\n    traits:\n"
                          "      mode: {values: {off: 1, med: 11}}\n")
    with pytest.raises(ValueError, match="quote"):
        parse_control_registry(data)


# ── OVR latch (2026-10-04): announce it, refuse into it, clear it by power-cycle ────────────────────

def _erv_cfg():
    import yaml
    return parse_control_registry(yaml.safe_load(open("config-examples/control.example.yaml")))


def test_ovr_raises_an_alarm_with_a_clear_action_and_startup_is_info():
    from server.api.viewmodel import external_mode_alert
    tc = _erv_cfg()["erv_attic"].traits_cfg
    a = external_mode_alert(tc, {"mode": 2})
    assert a["level"] == "alarm" and a["clear"]["path"] == "/devices/{id}/power-cycle"
    s = external_mode_alert(tc, {"mode": 20})
    assert s["level"] == "info" and "clear" not in s
    assert external_mode_alert(tc, {"mode": 11}) is None


def test_mode_control_lists_external_states_so_the_ui_can_lock_buttons():
    c = build_controls(_erv_cfg()["erv_attic"].traits_cfg, manual=True)[0]
    assert {e["value"] for e in c["external"]} == {2, 20}
    assert not ({o["value"] for o in c["options"]} & {2, 20})   # never offered as a button


def test_power_cycle_always_restores_power_even_if_the_wait_fails():
    from server.api.control import handle_power_cycle
    sent = []

    def boom(s):
        raise RuntimeError("interrupted")
    with pytest.raises(RuntimeError):
        handle_power_cycle(_erv_cfg(), "erv_attic", lambda t, p: sent.append((t, p)), boom)
    assert sent == [("cmnd/erv_pm/POWER", "OFF"), ("cmnd/erv_pm/POWER", "ON")]


def test_power_cycle_happy_path_and_unconfigured_device():
    from server.api.control import handle_power_cycle
    sent, slept = [], []
    code, body = handle_power_cycle(_erv_cfg(), "erv_attic", lambda t, p: sent.append(p), slept.append)
    assert code == 200 and sent == ["OFF", "ON"] and slept == [10]
    code, _ = handle_power_cycle(_erv_cfg(), "lamp_office", lambda t, p: None, lambda s: None)
    assert code == 404
