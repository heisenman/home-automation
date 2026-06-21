"""Tests for the capability contract (server/control/traits.py)."""
from server.control import traits as T
from tests._harness import raises, run_module


def test_known_traits():
    assert set(T.known_traits()) == {"switchable", "ranged", "positionable", "lockable", "setpoint"}


def test_switchable_set():
    tr = T.get_trait("switchable")
    assert tr.validate_command("set", {"on": True}, {}) == {"on": True}
    assert tr.validate_command("set", {"on": "off"}, {}) == {"on": False}
    with raises(T.TraitError):
        tr.validate_command("set", {"on": "maybe"}, {})
    with raises(T.TraitError):
        tr.validate_command("toggle", {"on": True}, {})   # no such action


def test_ranged_bounds_and_integer():
    tr = T.get_trait("ranged")
    assert tr.validate_command("set", {"level": 50}, {}) == {"level": 50}
    assert tr.validate_command("set", {"level": 0}, {}) == {"level": 0}
    assert tr.validate_command("set", {"level": 100}, {}) == {"level": 100}
    with raises(T.TraitError):
        tr.validate_command("set", {"level": 101}, {})    # out of default range
    with raises(T.TraitError):
        tr.validate_command("set", {"level": 12.5}, {})   # non-integer
    # per-device config narrows the range
    with raises(T.TraitError):
        tr.validate_command("set", {"level": 90}, {"min": 0, "max": 80})


def test_lockable_actions_and_sensitivity():
    tr = T.get_trait("lockable")
    assert tr.validate_command("lock", {}, {}) == {"locked": True}
    assert tr.validate_command("unlock", {}, {}) == {"locked": False}
    assert tr.is_sensitive("unlock") is True       # unlock requires nonce + confirm
    assert tr.is_sensitive("lock") is False
    assert tr.safe_state({}) == {"locked": True}   # fail-safe is LOCKED


def test_setpoint_range_and_unit_cfg():
    tr = T.get_trait("setpoint")
    cfg = {"min": 10, "max": 30, "unit": "degC"}
    assert tr.validate_command("set", {"value": 21.5}, cfg) == {"value": 21.5}
    with raises(T.TraitError):
        tr.validate_command("set", {"value": 40}, cfg)


def test_safe_states():
    assert T.get_trait("switchable").safe_state({}) == {"on": False}
    assert T.get_trait("switchable").safe_state({"safe_on": True}) == {"on": True}
    assert T.get_trait("ranged").safe_state({"safe_level": 25}) == {"level": 25}
    assert T.get_trait("lockable").safe_state({}) == {"locked": True}


def test_validate_device_traits():
    T.validate_device_traits(["switchable", "lockable"])    # ok
    with raises(T.TraitError):
        T.validate_device_traits(["switchable", "bogus"])


if __name__ == "__main__":
    run_module(globals())
