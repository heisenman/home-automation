"""Tests for the comms-event vocabulary + pure health logic (server/comms/events.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.comms import events as E  # noqa: E402
from tests._harness import run_module, raises  # noqa: E402

NOW = 1_000_000.0


def test_rejects_unknown_kind():
    with raises(E.CommsEventError):
        E.event(NOW, "d", "midea-lan", "exploded")


def test_accepts_known_kinds():
    e = E.event(NOW, "dehumidifier_office", "midea-lan", E.ACKED, "running=True")
    assert e.kind == "acked" and e.device_id == "dehumidifier_office"


def test_from_pull_outcome_mapping():
    assert E.from_pull_outcome(True) == E.REACHABLE
    assert E.from_pull_outcome(False, "connect_fail:BleakDeviceNotFoundError") == E.UNREACHABLE
    assert E.from_pull_outcome(False, "empty_buffer") == E.DEGRADED
    assert E.from_pull_outcome(False, "token expired") == E.AUTH_EXPIRED


def test_from_issue_status_mapping():
    assert E.from_issue_status("ok") == E.ACKED
    assert E.from_issue_status("no-ack") == E.NO_ACK
    assert E.from_issue_status("rejected") == E.REFUSED
    assert E.from_issue_status("mismatch") == E.DEGRADED


def test_health_unknown_when_no_recent():
    old = [E.event(NOW - 5000, "d", "t", E.ACKED)]
    assert E.health(old, NOW, recent_s=900) == "unknown"


def test_health_latest_event_wins():
    evs = [E.event(NOW - 100, "d", "t", E.ACKED),
           E.event(NOW - 10, "d", "t", E.UNREACHABLE)]      # newer = offline
    assert E.health(evs, NOW) == "offline"
    evs2 = [E.event(NOW - 100, "d", "t", E.UNREACHABLE),
            E.event(NOW - 10, "d", "t", E.REACHABLE)]         # newer = online
    assert E.health(evs2, NOW) == "online"


def test_health_degraded():
    evs = [E.event(NOW - 5, "d", "t", E.STALE)]
    assert E.health(evs, NOW) == "degraded"


def test_is_actionable_fail_safe():
    online = [E.event(NOW - 5, "d", "t", E.REACHABLE)]
    offline = [E.event(NOW - 5, "d", "t", E.UNREACHABLE)]
    stale = [E.event(NOW - 5, "d", "t", E.STALE)]
    assert E.is_actionable(online, NOW) is True
    assert E.is_actionable([], NOW) is True                  # unknown -> don't block (no evidence)
    assert E.is_actionable(offline, NOW) is False
    assert E.is_actionable(stale, NOW) is False


if __name__ == "__main__":
    run_module(globals())
