"""Actuator ids are resolved from control.yaml by device_type, not hardcoded (ADR-0026).

Guards the config-driven wiring that lets device_relocate/device_migrate rename an actuator with no code
edit: the Midea + Levoit ids come from `device_type`, so a renamed control.yaml key flows straight through.
"""
from server.control import bootstrap
from server.control.registry import parse_control_registry

REG = {"devices": {
    "dehumidifier_living_room": {"node": "server", "area": "living_room",
                                 "device_type": "dehumidifier", "traits": {"switchable": {}}},
    "purifier_h_office": {"node": "server", "area": "h_office",
                          "device_type": "air_purifier", "traits": {"switchable": {}}},
    "host_210": {"node": "server", "area": "h_office", "traits": {"indicator": {}}},
}}


def _air_purifier_id(reg):
    # mirrors the derivation in controller.main()
    return next((d for d, c in reg.items() if getattr(c, "device_type", None) == "air_purifier"), None)


def test_midea_and_levoit_resolved_by_type():
    reg = parse_control_registry(REG)
    assert bootstrap.midea_device_id_of(reg) == "dehumidifier_living_room"
    assert _air_purifier_id(reg) == "purifier_h_office"


def test_resolution_survives_a_rename():
    """The whole point: rename the keys, and resolution still finds them by type (no code/id coupling)."""
    renamed = {"devices": {
        "dehum_anything": {"node": "server", "area": "attic", "device_type": "dehumidifier",
                           "traits": {"switchable": {}}},
        "purifier_anything": {"node": "server", "area": "attic", "device_type": "air_purifier",
                              "traits": {"switchable": {}}},
    }}
    reg = parse_control_registry(renamed)
    assert bootstrap.midea_device_id_of(reg) == "dehum_anything"
    assert _air_purifier_id(reg) == "purifier_anything"


def test_none_when_actuator_absent():
    reg = parse_control_registry({"devices": {
        "host_210": {"node": "server", "area": "h_office", "traits": {"indicator": {}}}}})
    assert bootstrap.midea_device_id_of(reg) is None
    assert _air_purifier_id(reg) is None


if __name__ == "__main__":
    from tests._harness import run_module
    run_module(globals())
