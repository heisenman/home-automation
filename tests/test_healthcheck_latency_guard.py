"""Tests for the keepalived track_script latency guard (board healthcheck-latency-guard).

The bug this guards against was invisible for four days, so the tests care most about the cases where a
broken thing LOOKS fine: a probe that always fails, and a weight that cannot actually move a VIP.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tools import healthcheck_latency_guard as G  # noqa: E402

CONF = """
global_defs { router_id ha }

vrrp_script chk_dictator {
    script "/home/visko/home_automation/failover/healthcheck.sh"
    interval 5
    timeout 4
    fall 2
    rise 2
    weight -40                 # an unfit dictator drops below the peer's priority -> failover
}

vrrp_instance HA_DICTATOR {
    state MASTER
    interface enp4s0
    priority 150
    virtual_ipaddress {
        192.168.0.200/24
    }
    track_script {
        chk_dictator
    }
}

vrrp_script chk_dictator_airgap {
    script "/home/visko/ha-airgap-standby/failover/healthcheck.sh"
    interval 5
    timeout 4
    weight -60
}

vrrp_instance HA_DICTATOR_AIRGAP {
    state BACKUP
    interface wlp2s0
    priority 100
    track_script {
        chk_dictator_airgap
    }
}
"""


def _conf(tmp_path):
    p = tmp_path / "keepalived.conf"
    p.write_text(CONF)
    return p


def _leg(tmp_path, name="HA_DICTATOR"):
    return next(l for l in G.parse_keepalived(_conf(tmp_path)) if l["instance"] == name)


def test_parses_both_legs_from_live_config(tmp_path):
    legs = G.parse_keepalived(_conf(tmp_path))
    assert {l["instance"] for l in legs} == {"HA_DICTATOR", "HA_DICTATOR_AIRGAP"}
    h = next(l for l in legs if l["instance"] == "HA_DICTATOR")
    assert h["timeout"] == 4 and h["weight"] == -40 and h["priority"] == 150 and h["state"] == "MASTER"


def test_comment_after_a_value_is_not_parsed_as_config(tmp_path):
    """`weight -40  # ... -> failover` must yield -40, not choke on the trailing prose."""
    assert _leg(tmp_path)["weight"] == -40


def test_quiet_when_fast_and_passing(tmp_path):
    leg = _leg(tmp_path, "HA_DICTATOR_AIRGAP")
    samples = [(0.0, 0.12, 0)] * 20
    assert G.assess(leg, samples, 0.5) == []


def test_warns_at_half_the_timeout_before_it_is_crossed(tmp_path):
    """The whole point: alert with headroom, not after the VIP is already stuck."""
    leg = _leg(tmp_path, "HA_DICTATOR_AIRGAP")
    kinds = [f["kind"] for f in G.assess(leg, [(0.0, 2.1, 0)], 0.5)]
    assert "healthcheck_latency_high" in kinds
    # ...and stays quiet just below the line
    assert G.assess(leg, [(0.0, 1.9, 0)], 0.5) == []


def test_critical_once_the_timeout_is_reached(tmp_path):
    leg = _leg(tmp_path, "HA_DICTATOR_AIRGAP")
    f = G.assess(leg, [(0.0, 4.03, 0)], 0.5)
    assert [x["severity"] for x in f] == ["critical"]
    assert f[0]["kind"] == "healthcheck_timeout_exceeded"


def test_detects_a_probe_that_always_fails(tmp_path):
    """The 2026-07-27 shape: fast enough, but never passes — so the VIP can never move."""
    leg = _leg(tmp_path, "HA_DICTATOR_AIRGAP")
    kinds = [f["kind"] for f in G.assess(leg, [(0.0, 0.2, 2)] * 12, 0.5)]
    assert "healthcheck_stuck_failing" in kinds


def test_one_success_means_not_stuck(tmp_path):
    leg = _leg(tmp_path, "HA_DICTATOR_AIRGAP")
    samples = [(0.0, 0.2, 2)] * 11 + [(0.0, 0.2, 0)]
    assert "healthcheck_stuck_failing" not in [f["kind"] for f in G.assess(leg, samples, 0.5)]


def test_flags_a_weight_too_small_to_yield_the_vip(tmp_path):
    """150-40=110 > a standby's 100 — health-based failover disarmed by arithmetic."""
    kinds = [f["kind"] for f in G.assess(_leg(tmp_path), [(0.0, 0.1, 0)], 0.5)]
    assert "healthcheck_weight_ineffective" in kinds


def test_sufficient_weight_is_not_flagged(tmp_path):
    leg = _leg(tmp_path)
    leg["weight"] = -60                      # 150-60=90 < 100 -> actually yields
    kinds = [f["kind"] for f in G.assess(leg, [(0.0, 0.1, 0)], 0.5)]
    assert "healthcheck_weight_ineffective" not in kinds


def test_backup_leg_is_not_weight_checked(tmp_path):
    """Only a MASTER has to fall below a standby; a BACKUP raising/lowering itself is not this bug."""
    leg = _leg(tmp_path, "HA_DICTATOR_AIRGAP")
    kinds = [f["kind"] for f in G.assess(leg, [(0.0, 0.1, 0)], 0.5)]
    assert "healthcheck_weight_ineffective" not in kinds


def test_no_samples_never_fabricates_a_latency_finding(tmp_path):
    """A silent stat file means the probe is not reporting — it must not read as 'fast and healthy',
    but it also must not invent a latency number."""
    leg = _leg(tmp_path, "HA_DICTATOR_AIRGAP")
    assert [f for f in G.assess(leg, [], 0.5) if "latency" in f["kind"]] == []


def test_sample_file_roundtrip_and_truncate(tmp_path):
    p = tmp_path / "stat"
    p.write_text("100.0 100.5 0\n100.5 102.6 2\ngarbage line\n\n")
    s = G.read_samples(p)
    assert len(s) == 2
    assert abs(s[1][1] - 2.1) < 1e-6 and s[1][2] == 2
    assert p.read_text() == ""                      # truncated, so the next run sees only new samples
    assert G.read_samples(p) == []


def test_read_samples_can_preserve(tmp_path):
    p = tmp_path / "stat"
    p.write_text("1.0 1.5 0\n")
    assert len(G.read_samples(p, truncate=False)) == 1
    assert p.read_text() != ""


def test_stat_path_follows_the_owning_checkout():
    """.210 runs two legs from two SEPARATE checkouts; samples must not collide in one file."""
    a = G.stat_path_for("/home/visko/home_automation/failover/healthcheck.sh")
    b = G.stat_path_for("/home/visko/ha-airgap-standby/failover/healthcheck.sh")
    assert a != b
    assert a.parents[1].name == "home_automation" and b.parents[1].name == "ha-airgap-standby"


def test_missing_config_is_not_an_error(tmp_path):
    assert G.parse_keepalived(tmp_path / "nope.conf") == []
