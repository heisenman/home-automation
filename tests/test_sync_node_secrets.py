"""tools/sync_node_secrets.plan — the pure merge rule behind keeping .210's and ha-2's node LUTs identical."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import sync_node_secrets as SY  # noqa: E402


def _n(secret):
    return {"cmd_secret": secret, "mqtt_pass": "x"}


def test_union_in_both_directions():
    to_remote, to_local, conflicts = SY.plan({"a": _n("1"), "b": _n("2")}, {"b": _n("2"), "c": _n("3")}, None)
    assert set(to_remote) == {"a"} and set(to_local) == {"c"} and conflicts == []


def test_a_conflicting_secret_is_never_resolved_silently():
    to_remote, to_local, conflicts = SY.plan({"a": _n("1")}, {"a": _n("9")}, None)
    assert conflicts == ["a"] and not to_remote and not to_local


def test_prefer_resolves_a_conflict_toward_the_named_side():
    to_remote, _, conflicts = SY.plan({"a": _n("1")}, {"a": _n("9")}, "local")
    assert to_remote == {"a": _n("1")} and conflicts == []
    _, to_local, conflicts = SY.plan({"a": _n("1")}, {"a": _n("9")}, "remote")
    assert to_local == {"a": _n("9")} and conflicts == []


def test_identical_entries_need_nothing():
    assert SY.plan({"a": _n("1")}, {"a": _n("1")}, None) == ({}, {}, [])
