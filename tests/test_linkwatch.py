"""Tests for the air-gap link watchdog's pure decision core (server/cluster/linkwatch.py, ADR-0039).

Everything here drives `decide()` — no radio, no bus, no clock. The behaviours that matter are the ones
that were expensive to learn on 2026-09-02:

  * hysteresis      — a single dropped probe must not bounce the radio (WiFi drops packets)
  * discrimination  — gateway up + peer down is HA-2's fault; reassociating cannot fix it, so we must not
  * bounded effort  — reassociation is tried a few times, then we escalate and STOP touching the radio
  * edge-triggering — one alert per outage, not one per tick, and re-armed for the next outage
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.cluster import linkwatch as lw  # noqa: E402

NOW = 1_000_000.0
UP = lw.Probes(gateway_ok=True, peer_ok=True)
DOWN = lw.Probes(gateway_ok=False, peer_ok=False)
PEER_ONLY_DOWN = lw.Probes(gateway_ok=True, peer_ok=False)


def kinds(d):
    return [a["kind"] for a in d.alerts]


def drive(probes, state, now=NOW, **kw):
    """One tick."""
    return lw.decide(probes, state, now, **kw)


def down_until_action(state, start=NOW):
    """Tick DOWN until the watchdog acts; return (decision, tick_count)."""
    for i in range(1, lw.FAIL_TICKS + 2):
        d = drive(DOWN, state, start + i)
        if d.action != "none":
            return d, i
    return d, lw.FAIL_TICKS + 1


# ── the healthy path ───────────────────────────────────────────────────────────────────────────────
def test_link_up_is_quiet():
    s = {}
    d = drive(UP, s)
    assert d.link == "up" and d.action == "none" and d.alerts == []


def test_single_failure_does_not_act():
    """One dropped probe is normal WiFi. Acting on it would bounce the radio constantly."""
    s = {}
    d = drive(DOWN, s, NOW + 1)
    assert d.link == "down" and d.action == "none" and d.alerts == []
    assert s["consecutive_down"] == 1


def test_below_threshold_stays_quiet():
    s = {}
    for i in range(1, lw.FAIL_TICKS):
        d = drive(DOWN, s, NOW + i)
        assert d.action == "none", f"acted early at tick {i}"
        assert d.alerts == [], f"alerted early at tick {i}"


# ── the outage path ────────────────────────────────────────────────────────────────────────────────
def test_sustained_outage_reassociates_and_alerts():
    s = {}
    d, n = down_until_action(s)
    assert n == lw.FAIL_TICKS, f"acted at tick {n}, expected {lw.FAIL_TICKS}"
    assert d.action == "reassociate"
    assert "airgap_link_down" in kinds(d)
    assert [a for a in d.alerts if a["kind"] == "airgap_link_down"][0]["severity"] == "critical"


def test_outage_alert_fires_once_not_every_tick():
    s = {}
    down_until_action(s)
    later = drive(DOWN, s, NOW + 50)                    # still down, inside cooldown
    assert "airgap_link_down" not in kinds(later)


def test_outage_alert_survives_a_skipped_tick():
    """A crash or a slow reassociate can step the counter straight past FAIL_TICKS. Latching the edge on a
    flag (not `n == FAIL_TICKS`) is what keeps a real outage from going unannounced."""
    s = {"consecutive_down": lw.FAIL_TICKS + 5}         # as if several ticks were missed
    d = drive(DOWN, s, NOW + 1)
    assert "airgap_link_down" in kinds(d)


def test_cooldown_blocks_a_second_reassociation():
    s = {}
    down_until_action(s)
    d = drive(DOWN, s, NOW + lw.FAIL_TICKS + 1)         # immediately after — inside COOLDOWN_S
    assert d.action == "none" and "cooldown" in d.note


def test_second_reassociation_after_cooldown():
    s = {}
    d1, n = down_until_action(s)
    assert d1.action == "reassociate"
    d2 = drive(DOWN, s, NOW + n + lw.COOLDOWN_S + 1)
    assert d2.action == "reassociate"
    assert s["reassoc_attempts"] == 2


# ── bounded effort: stop when reassociating clearly isn't the fix ───────────────────────────────────
def test_gives_up_and_screams_after_max_reassociations():
    s = {"consecutive_down": lw.FAIL_TICKS, "reassoc_attempts": lw.MAX_REASSOC,
         "link_down_alerted": True, "link_down_since": NOW}
    d = drive(DOWN, s, NOW + 999)
    assert d.action == "scream"
    assert "airgap_link_unrecovered" in kinds(d)
    assert s["escalated"] is True


def test_after_escalation_it_stops_acting():
    """Hammering a radio that isn't the problem only makes a human's diagnosis harder."""
    s = {"consecutive_down": lw.FAIL_TICKS, "reassoc_attempts": lw.MAX_REASSOC,
         "link_down_alerted": True, "escalated": True, "link_down_since": NOW}
    d = drive(DOWN, s, NOW + 9999)
    assert d.action == "none" and d.alerts == []


# ── the discrimination that keeps us from bouncing the radio for someone else's fault ───────────────
def test_peer_down_with_healthy_gateway_does_not_reassociate():
    """Gateway answers => the link carries traffic => ha-2 is the fault. Restarting our own WiFi cannot
    fix a dead peer; doing it anyway is the shared-fate error required-services.yaml warns about."""
    s = {}
    for i in range(1, lw.FAIL_TICKS + 3):
        d = drive(PEER_ONLY_DOWN, s, NOW + i)
        assert d.action == "none", "must never touch the radio while the gateway answers"
    assert d.link == "peer_down"
    assert s.get("consecutive_down") == 0


def test_peer_down_alerts_once_then_recovers():
    s = {}
    d1 = drive(PEER_ONLY_DOWN, s, NOW)
    assert "airgap_peer_down" in kinds(d1)
    d2 = drive(PEER_ONLY_DOWN, s, NOW + 30)
    assert kinds(d2) == []                              # edge-triggered, not per-tick
    d3 = drive(UP, s, NOW + 60)
    assert "airgap_peer_up" in kinds(d3)


def test_peer_reachable_by_tcp_only_still_counts_as_up():
    """observe() ORs ping with a TCP connect so ICMP filtering can't fake an outage."""
    d = drive(lw.Probes(gateway_ok=False, peer_ok=True), {}, NOW)
    assert d.link == "up" and d.action == "none"


# ── recovery ───────────────────────────────────────────────────────────────────────────────────────
def test_recovery_alerts_and_resets():
    s = {}
    down_until_action(s)
    d = drive(UP, s, NOW + 300)
    assert "airgap_link_up" in kinds(d)
    assert s["consecutive_down"] == 0
    assert s["link_down_alerted"] is False              # re-armed for the next outage
    assert s["escalated"] is False


def test_recovery_alert_only_after_a_real_outage():
    """A blip that never crossed the threshold shouldn't announce a recovery nobody was told about."""
    s = {}
    drive(DOWN, s, NOW + 1)                             # 1 tick — below threshold
    d = drive(UP, s, NOW + 2)
    assert kinds(d) == []


def test_flapping_does_not_refill_the_reassociation_budget():
    """A link that comes up for one tick between failures must not reset the budget, or a flapping leg
    gets its radio bounced forever."""
    s = {}
    down_until_action(s)
    assert s["reassoc_attempts"] == 1
    drive(UP, s, NOW + lw.FAIL_TICKS + 1)               # brief recovery, well under STABLE_S
    assert s["reassoc_attempts"] == 1, "budget refilled by a flap"


def test_stable_recovery_does_refill_the_budget():
    s = {}
    down_until_action(s)
    drive(UP, s, NOW + 500)                             # recovery -> link_up_since = 500
    drive(UP, s, NOW + 500 + lw.STABLE_S + 1)           # stably up past STABLE_S
    assert s["reassoc_attempts"] == 0


# ── deploy safety ──────────────────────────────────────────────────────────────────────────────────
def test_maintenance_inhibit_holds_remediation_but_still_alerts():
    """A deliberate deploy bounces services; the watchdog must not fight it — but it must still say the
    link is down, because a deploy is exactly when you want to know that."""
    s = {}
    for i in range(1, lw.FAIL_TICKS + 1):
        d = drive(DOWN, s, NOW + i, inhibited=True)
    assert d.action == "none" and "maintenance" in d.note
    assert "airgap_link_down" in kinds(d)


if __name__ == "__main__":
    from tests._harness import run_module
    raise SystemExit(run_module(globals()))
