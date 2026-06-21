"""Tests for the control registry / secrets loader (server/control/registry.py)."""
from server.control import registry as R
from server.control import traits
from tests._harness import raises, run_module

DATA = {
    "version": 1,
    "devices": {
        "lamp_office": {"node": "c6-bench", "area": "c_office",
                        "traits": {"switchable": {}, "ranged": {"min": 0, "max": 100}}},
        "lock_front": {"node": "c6-bench", "area": "entry", "traits": {"lockable": {}}},
    },
}


def test_parse_registry():
    reg = R.parse_control_registry(DATA)
    assert set(reg) == {"lamp_office", "lock_front"}
    assert reg["lamp_office"].area == "c_office"
    assert reg["lamp_office"].traits_cfg["ranged"] == {"min": 0, "max": 100}
    assert reg["lock_front"].node == "c6-bench"


def test_unknown_trait_rejected():
    bad = {"devices": {"x": {"node": "n", "area": "a", "traits": {"bogus": {}}}}}
    with raises(traits.TraitError):
        R.parse_control_registry(bad)


def test_missing_node_or_area_rejected():
    bad = {"devices": {"x": {"area": "a", "traits": {"switchable": {}}}}}
    with raises(ValueError):
        R.parse_control_registry(bad)


def test_empty_registry():
    assert R.parse_control_registry({}) == {}


def test_check_secrets_present():
    reg = R.parse_control_registry(DATA)
    missing = R.check_secrets_present(reg, {"lamp_office": "s"})
    assert missing == ["lock_front"]


if __name__ == "__main__":
    run_module(globals())
