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


def test_secrets_from_lut_maps_by_node():
    # both devices live on node c6-bench → both get that node's cmd_secret
    reg = R.parse_control_registry(DATA)
    lut = {"c6-bench": {"cmd_secret": "NODEKEY", "mqtt_pass": "x"}}
    secrets = R.secrets_from_lut(reg, lut)
    assert secrets == {"lamp_office": "NODEKEY", "lock_front": "NODEKEY"}


def test_secrets_from_lut_skips_unenrolled_or_secretless_nodes():
    data = {"devices": {
        "a": {"node": "node_a", "area": "x", "traits": {"switchable": {}}},
        "b": {"node": "node_b", "area": "x", "traits": {"switchable": {}}},   # not in LUT
        "c": {"node": "node_c", "area": "x", "traits": {"switchable": {}}},   # in LUT but no cmd_secret
    }}
    reg = R.parse_control_registry(data)
    lut = {"node_a": {"cmd_secret": "K"}, "node_c": {"mqtt_pass": "p"}}
    secrets = R.secrets_from_lut(reg, lut)
    assert secrets == {"a": "K"}                      # b and c omitted → uncommandable until enrolled
    assert R.check_secrets_present(reg, secrets) == ["b", "c"]


if __name__ == "__main__":
    run_module(globals())
