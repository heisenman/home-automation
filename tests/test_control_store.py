"""Tests for the controller state store (server/control/control_store.py)."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.control import control_store as S  # noqa: E402
from tests._harness import run_module  # noqa: E402


def _db():
    c = sqlite3.connect(":memory:")
    S.ensure_schema(c)
    return c


def test_policy_seed_then_app_edit_wins():
    c = _db()
    S.seed_policy(c, "dehumidifier_office", {"control": {"on_above": 44}})
    S.seed_policy(c, "dehumidifier_office", {"control": {"on_above": 99}})   # seed again → no-op
    assert S.get_policy(c, "dehumidifier_office")["control"]["on_above"] == 44
    S.set_policy(c, "dehumidifier_office", {"control": {"on_above": 50}})    # explicit edit → wins
    assert S.get_policy(c, "dehumidifier_office")["control"]["on_above"] == 50


def test_all_policies():
    c = _db()
    S.set_policy(c, "a", {"x": 1})
    S.set_policy(c, "b", {"y": 2})
    assert set(S.all_policies(c)) == {"a", "b"}


def test_cycle_transition_tracks_on_and_off():
    c = _db()
    assert S.get_cycle(c, "d") == (None, None)
    S.record_transition(c, "d", running=True, ts=100.0)
    assert S.get_cycle(c, "d") == (100.0, None)
    S.record_transition(c, "d", running=False, ts=200.0)
    assert S.get_cycle(c, "d") == (100.0, 200.0)            # on preserved, off updated
    S.record_transition(c, "d", running=True, ts=300.0)
    assert S.get_cycle(c, "d") == (300.0, 200.0)


def test_override_set_get_expiry_clear():
    c = _db()
    assert S.get_override(c, "d") is None
    S.set_override(c, "d", "off", expiry=1000.0)
    assert S.get_override(c, "d", now=900.0) == ("off", 1000.0)   # active
    assert S.get_override(c, "d", now=1001.0) is None             # expired
    S.set_override(c, "d", "boost_on", expiry=None)
    assert S.get_override(c, "d", now=9e9) == ("boost_on", None)  # until cleared
    S.clear_override(c, "d")
    assert S.get_override(c, "d") is None


def test_control_log_append_and_recent_order():
    c = _db()
    S.append_log(c, "d", desired=True, source="rule", reason="RH high", acted=True, status="ok")
    S.append_log(c, "d", desired=False, source="override", reason="off 2h", acted=True, status="ok")
    rows = S.recent_log(c, "d", limit=10)
    assert len(rows) == 2 and rows[0]["source"] == "override"     # newest first
    assert rows[0]["desired"] == 0 and rows[1]["desired"] == 1


if __name__ == "__main__":
    run_module(globals())
