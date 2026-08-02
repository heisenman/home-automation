"""ADR-0036 intake auto-registration (server/api/control.py::_register_edge_node_device).

This path runs when an operator adopts new hardware from the PWA, and the north star is a house the
operator runs WITHOUT expert help. So the property under test is not just "produces a correct record" —
it is **never hands back a decision the server could have made itself**. Each test below pins a case that
did exactly that on 2026-08-02.
"""
import textwrap

import pytest

from server.api import control as C

yaml = pytest.importorskip("yaml")


def _registry(tmp_path, devices: dict | None = None):
    p = tmp_path / "devices.yaml"
    p.write_text(textwrap.dedent("""\
        # test registry
        devices:
        """) + (yaml.safe_dump({"devices": devices}).split("devices:", 1)[1] if devices else "\n"))
    return p


def _read(p):
    return (yaml.safe_load(p.read_text()) or {}).get("devices") or {}


# ── device_id derivation ──────────────────────────────────────────────────────────────────────────

def test_preferred_id_when_room_is_empty():
    assert C._gas_device_id(set(), "mech_closet", "sgp41_gas") == "gas_mech_closet"


def test_second_gas_sensor_in_a_room_qualifies_by_family():
    """Redundant sensing in one room is a DESIGN GOAL, so a taken gas_<area> is expected input, not an
    error. An SGP41 beside a BME680 measures VOC+NOx the BME680 cannot."""
    taken = {"gas_mech_closet"}
    assert C._gas_device_id(taken, "mech_closet", "sgp41_gas") == "gas_mech_closet_sgp41"


def test_third_same_family_sensor_gets_a_numeric_tail_and_terminates():
    taken = {"gas_attic", "gas_attic_sgp41", "gas_attic_sgp41_2"}
    assert C._gas_device_id(taken, "attic", "sgp41_gas") == "gas_attic_sgp41_3"


def test_derivation_is_deterministic():
    taken = {"gas_shop"}
    a = C._gas_device_id(taken, "shop", "bme680_gas")
    b = C._gas_device_id(taken, "shop", "bme680_gas")
    assert a == b == "gas_shop_bme680"


# ── registration ──────────────────────────────────────────────────────────────────────────────────

def test_registers_new_node_born_dormant(tmp_path):
    p = _registry(tmp_path)
    did, err = C._register_edge_node_device(p, "sgp41_mech", ["sgp41_gas"], "mech_closet")
    assert (did, err) == ("gas_mech_closet", None)
    rec = _read(p)["sgp41_mech-gas"]
    assert rec["area"] == C.DORMANT_AREA          # dormant until the relocate wakes it
    assert rec["node_id"] == "sgp41_mech"
    assert rec["device_type"] == "sgp41_gas"
    assert "nox_index" in rec["capabilities"]     # the SGP41's second pixel


def test_occupied_room_registers_anyway_rather_than_demanding_a_manual_id(tmp_path):
    """The 2026-08-02 dead end: mech_closet already held a BME680 on gas_mech_closet, and intake replied
    'Register this node manually with a distinct device_id' — a hand-edit of prod YAML, for the ordinary
    case of adding a second sensor to a room."""
    p = _registry(tmp_path, {"standby_c6-gas": {"device_id": "gas_mech_closet", "node_id": "standby_c6",
                                                "device_type": "bme680_gas", "area": "mech_closet"}})
    did, err = C._register_edge_node_device(p, "sgp41_mech", ["sgp41_gas"], "mech_closet")
    assert err is None
    assert did == "gas_mech_closet_sgp41"
    devices = _read(p)
    assert devices["standby_c6-gas"]["device_id"] == "gas_mech_closet"   # incumbent untouched — no
    assert devices["sgp41_mech-gas"]["device_id"] == "gas_mech_closet_sgp41"  # history migration needed


def test_preview_predicts_the_id_the_apply_will_use(tmp_path):
    """dry_run used to return before the registry was even opened, so a preview reported a clean plan the
    apply then rejected. Preview and apply must agree, collision or not."""
    p = _registry(tmp_path, {"standby_c6-gas": {"device_id": "gas_mech_closet", "node_id": "standby_c6",
                                                "device_type": "bme680_gas", "area": "mech_closet"}})
    preview, err = C._register_edge_node_device(p, "sgp41_mech", ["sgp41_gas"], "mech_closet", dry_run=True)
    assert err is None
    assert "sgp41_mech-gas" not in _read(p)       # a dry run writes nothing
    applied, err = C._register_edge_node_device(p, "sgp41_mech", ["sgp41_gas"], "mech_closet")
    assert (applied, err) == (preview, None)


def test_re_registering_the_same_node_is_idempotent(tmp_path):
    p = _registry(tmp_path)
    first, _ = C._register_edge_node_device(p, "sgp41_mech", ["sgp41_gas"], "mech_closet")
    again, err = C._register_edge_node_device(p, "sgp41_mech", ["sgp41_gas"], "mech_closet")
    assert (again, err) == (first, None)
    assert len(_read(p)) == 1


# ── the two "cannot resolve" cases, which must be TOLD APART ──────────────────────────────────────

def test_unknown_gas_ability_is_reported_as_server_drift_not_as_a_relay_node(tmp_path):
    """A stale air-gapped server used to answer a healthy newer node with the relay-only verdict, which
    reads as a statement about the HARDWARE. It must point at the server instead."""
    p = _registry(tmp_path)
    did, err = C._register_edge_node_device(p, "future_node", ["sgp99_gas"], "attic")
    assert did is None
    assert "sgp99_gas" in err
    assert "THIS SERVER BUILD" in err
    assert "do not re-flash" in err.lower()
    assert "relay" not in err.lower()             # the misleading verdict must NOT appear


def test_genuinely_relay_only_node_still_reports_relay_only(tmp_path):
    p = _registry(tmp_path)
    did, err = C._register_edge_node_device(p, "relay_hall", ["ble_relay"], "hall")
    assert did is None
    assert "relay-only" in err
    assert "THIS SERVER BUILD" not in err
    assert not _read(p)                           # and registers nothing
