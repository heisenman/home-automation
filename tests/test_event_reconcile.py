"""ADR-0023 inverse — event-driven reconcile (server/mesh/coordinator.py).

Pins the pure decision core: parse_event (topic/payload → node,state), EventReconciler (relaying
transition → intent, asymmetric drop/return), and EventDispatcher (coalesce + per-node rate-limit with
an injected clock). The threading/MQTT shell in main() is a thin wrapper over these."""
import json

from server.mesh.coordinator import (
    DEFAULT_DWELL_S,
    EventDispatcher,
    EventReconciler,
    parse_event,
)


def _ev(node="d1001-beachhead", on_wall=True, relaying=True, censusing=False, ts=1):
    return json.dumps({"node": node, "kind": "power",
                       "state": {"on_wall": on_wall, "relaying": relaying, "censusing": censusing}, "ts": ts})


# ── parse_event ──────────────────────────────────────────────────────────────────────────────────────
def test_parse_event_extracts_node_and_state():
    node, state = parse_event("home/edge/d1001-beachhead/event", _ev(relaying=False))
    assert node == "d1001-beachhead"
    assert state == {"on_wall": True, "relaying": False, "censusing": False}


def test_parse_event_rejects_bad_topic_and_payload():
    assert parse_event("home/edge/x/reach", _ev()) == (None, None)          # wrong leaf
    assert parse_event("home/edge/x/event", "not json") == (None, None)     # unparseable
    assert parse_event("home/edge/x/event", json.dumps({"no": "state"})) == (None, None)
    assert parse_event("home/edge/x/event", json.dumps([1, 2])) == (None, None)   # not a dict


# ── EventReconciler: transitions only, asymmetric ─────────────────────────────────────────────────────
def test_first_event_is_not_a_transition():
    r = EventReconciler()
    assert r.observe("n", {"relaying": True}) is None       # retained snapshot on subscribe → no storm


def test_relay_drop_is_urgent_census_then_reconcile_now():
    r = EventReconciler()
    r.observe("n", {"relaying": True})
    intent = r.observe("n", {"relaying": False})
    assert intent == {"node": "n", "dwell_s": 0.0, "census": True, "reason": "relay-drop"}


def test_relay_return_is_lazy_dwelled():
    r = EventReconciler(dwell_return=DEFAULT_DWELL_S)
    r.observe("n", {"relaying": False})
    intent = r.observe("n", {"relaying": True})
    assert intent["dwell_s"] == DEFAULT_DWELL_S and intent["census"] is False and intent["reason"] == "relay-return"


def test_no_relaying_change_yields_no_intent():
    r = EventReconciler()
    r.observe("n", {"relaying": True, "on_wall": True})
    assert r.observe("n", {"relaying": True, "on_wall": False}) is None   # on_wall flip alone: ignored


# ── EventDispatcher: coalesce + rate-limit with an injected clock ──────────────────────────────────────
def test_census_fires_immediately_once_per_window():
    d = EventDispatcher(EventReconciler(), coalesce_s=5.0, rate_limit_s=30.0)
    d.observe("n", {"relaying": True}, now=0.0)              # prime (no transition)
    assert d.observe("n", {"relaying": False}, now=1.0) == {"census_now": True}   # drop → census now
    # a second drop-ish event inside the same pending window does not re-fire the census
    assert d.observe("n", {"relaying": False}, now=2.0) is None


def test_reconcile_deferred_until_coalesce_elapses():
    d = EventDispatcher(EventReconciler(), coalesce_s=5.0)
    d.observe("n", {"relaying": True}, now=0.0)
    d.observe("n", {"relaying": False}, now=1.0)             # ready_at = 6.0
    assert d.due(now=5.9) == []                              # still coalescing
    got = d.due(now=6.0)
    assert [i["node"] for i in got] == ["n"] and got[0]["dwell_s"] == 0.0
    assert d.due(now=6.0) == []                              # consumed exactly once


def test_fresh_event_resets_coalesce_window():
    d = EventDispatcher(EventReconciler(), coalesce_s=5.0)
    d.observe("n", {"relaying": True}, now=0.0)
    d.observe("n", {"relaying": False}, now=1.0)             # ready_at 6.0
    d.observe("n", {"relaying": True}, now=4.0)              # newer intent → ready_at 9.0 (return, dwelled)
    assert d.due(now=6.0) == []                              # window pushed out by the later event
    got = d.due(now=9.0)
    assert got and got[0]["reason"] == "relay-return"        # newest intent wins


def test_rate_limit_defers_second_reconcile_per_node():
    d = EventDispatcher(EventReconciler(), coalesce_s=5.0, rate_limit_s=30.0)
    d.observe("n", {"relaying": True}, now=0.0)
    d.observe("n", {"relaying": False}, now=1.0)
    assert d.due(now=6.0)                                    # first reconcile fires at t=6
    d.observe("n", {"relaying": True}, now=7.0)              # return intent, ready_at 12.0
    assert d.due(now=12.0) == []                             # within 30s of last fire → deferred
    got = d.due(now=36.0)                                    # 6 + 30 = 36 → now allowed
    assert got and got[0]["node"] == "n"
