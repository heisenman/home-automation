"""Tests for the recording watch (server/maintenance/recording_watch.py, ADR-0040).

The behaviour that matters is the one ha-gap-watcher didn't have: **a device that stops reporting and
stays stopped must be detected.** gap_watcher pairs consecutive readings, so trailing silence produced no
pair and two gas sensors were dark for two days across four "0 gaps" runs. The regression test for that is
`test_the_2026_08_31_outage_would_now_be_caught`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.maintenance import recording_watch as rw  # noqa: E402

NOW = 1_000_000.0
HOUR = 3600.0
DAY = 86400.0


def exp(**kw):
    base = dict(default_stale_s=HOUR, dormant_after_s=7 * DAY, per_device={}, per_type={}, ignore=set())
    base.update(kw)
    return rw.Expectations(**base)


def row(did, age_s=None, dtype="sgp40_gas", area="kitchen", never=False):
    return rw.Row(did, dtype, area, None if never else NOW - age_s)


def states(sts):
    return {s.device_id: s.state for s in sts}


# ── the core regression ────────────────────────────────────────────────────────────────────────────
def test_the_2026_08_31_outage_would_now_be_caught():
    """gas_hbed + gas_kitchen, silent 62h, everything else healthy. ha-gap-watcher reported 0 gaps for
    four days running. This must report exactly those two."""
    roster = [row("gas_kitchen", 62 * HOUR), row("gas_hbed", 62 * HOUR),
              row("gas_c_bed", 9), row("gas_h_office", 7), row("meter_attic", 14)]
    s = rw.classify(roster, exp(), NOW)
    assert states(s)["gas_kitchen"] == rw.LATE
    assert states(s)["gas_hbed"] == rw.LATE
    assert [x.device_id for x in s if x.state == rw.LATE] == ["gas_hbed", "gas_kitchen"]


def test_healthy_fleet_is_all_ok():
    s = rw.classify([row("a", 10), row("b", 300), row("c", 59 * 60)], exp(), NOW)
    assert set(states(s).values()) == {rw.OK}


def test_just_over_threshold_is_late():
    s = rw.classify([row("a", HOUR + 1)], exp(), NOW)
    assert s[0].state == rw.LATE


def test_exactly_at_threshold_is_ok():
    s = rw.classify([row("a", HOUR)], exp(), NOW)
    assert s[0].state == rw.OK


# ── dormant: don't bury the real signal under retired hardware ─────────────────────────────────────
def test_long_dead_device_is_dormant_not_alerted():
    """e1001_c_office had been silent 885h. Alerting on that on day one would drown the two sensors that
    actually mattered."""
    s = rw.classify([row("e1001_c_office", 885 * HOUR)], exp(), NOW)
    assert s[0].state == rw.DORMANT


def test_dormant_boundary():
    assert rw.classify([row("a", 7 * DAY - 1)], exp(), NOW)[0].state == rw.LATE
    assert rw.classify([row("a", 7 * DAY + 1)], exp(), NOW)[0].state == rw.DORMANT


def test_never_reported_is_its_own_state():
    s = rw.classify([row("ghost", never=True)], exp(), NOW)
    assert s[0].state == rw.NEVER and s[0].age_s is None


# ── thresholds ─────────────────────────────────────────────────────────────────────────────────────
def test_per_device_override_wins():
    e = exp(per_device={"slowpoke": 24 * 60 * 60}, per_type={"sgp40_gas": 60})
    assert rw.classify([row("slowpoke", 5 * HOUR)], e, NOW)[0].state == rw.OK


def test_per_type_override_applies():
    e = exp(per_type={"sgp40_gas": 6 * HOUR})
    assert rw.classify([row("x", 5 * HOUR)], e, NOW)[0].state == rw.OK


def test_ignore_list_drops_the_device_entirely():
    s = rw.classify([row("a", 99 * HOUR), row("b", 5)], exp(ignore={"a"}), NOW)
    assert [x.device_id for x in s] == ["b"]


# ── alerts are edge-triggered ──────────────────────────────────────────────────────────────────────
def test_alert_fires_once_then_recovers_once():
    st = {}
    late = rw.classify([row("gas_kitchen", 62 * HOUR)], exp(), NOW)
    a1 = rw.diff_alerts(late, st)
    assert [a["kind"] for a in a1] == ["device_not_recording"]
    assert a1[0]["severity"] == "critical"
    assert rw.diff_alerts(late, st) == []                      # not every tick

    ok = rw.classify([row("gas_kitchen", 5)], exp(), NOW)
    a2 = rw.diff_alerts(ok, st)
    assert [a["kind"] for a in a2] == ["device_recording_ok"]
    assert rw.diff_alerts(ok, st) == []


def test_dormant_and_never_do_not_alert():
    st = {}
    s = rw.classify([row("old", 900 * HOUR), row("ghost", never=True)], exp(), NOW)
    assert rw.diff_alerts(s, st) == []


def test_state_does_not_leak_for_devices_that_leave_the_roster():
    st = {}
    rw.diff_alerts(rw.classify([row("gone", 62 * HOUR)], exp(), NOW), st)
    assert "gone" in st
    rw.diff_alerts(rw.classify([row("other", 5)], exp(), NOW), st)
    assert "gone" not in st


# ── the twice-daily digest ─────────────────────────────────────────────────────────────────────────
def _at(h, day=2):
    import datetime as dt
    return dt.datetime(2026, 9, day, h, 30, tzinfo=dt.timezone.utc).timestamp()


def test_digest_fires_once_per_slot():
    st = {}
    assert rw.digest_due(_at(8), [8, 20], st) is True
    assert rw.digest_due(_at(9), [8, 20], st) is False        # same slot
    assert rw.digest_due(_at(20), [8, 20], st) is True        # next slot
    assert rw.digest_due(_at(21), [8, 20], st) is False


def test_digest_slot_resets_next_day():
    st = {}
    assert rw.digest_due(_at(20, day=2), [8, 20], st) is True
    assert rw.digest_due(_at(8, day=3), [8, 20], st) is True


def test_digest_not_due_before_the_first_slot():
    assert rw.digest_due(_at(3), [8, 20], {}) is False


def test_missed_tick_still_fires_late_rather_than_skipping():
    """The digest IS the 'still alive' signal — dropping one silently defeats its purpose."""
    st = {}
    assert rw.digest_due(_at(19), [8, 20], st) is True        # 08:00 slot, fired late at 19:30
    assert rw.digest_due(_at(20), [8, 20], st) is True        # 20:00 slot still fires


def test_digest_says_all_clear_when_healthy():
    d = rw.build_digest(rw.classify([row("a", 5), row("b", 9)], exp(), NOW), NOW)
    assert d["severity"] == "info" and "all 2 active devices recording" in d["message"]
    assert d["ok"] == 2 and d["late"] == []


def test_healthy_digest_does_not_overclaim_when_something_is_dormant():
    """'all devices recording' while one is dormant trains the reader to discount the digest."""
    d = rw.build_digest(rw.classify([row("a", 5), row("old", 900 * HOUR)], exp(), NOW), NOW)
    assert d["severity"] == "info"                       # dormant is not an alert...
    assert "1 inactive" in d["message"]                  # ...but it is not hidden either
    assert "old" in d["message"]


def test_digest_names_the_broken_devices():
    s = rw.classify([row("gas_kitchen", 62 * HOUR), row("ok1", 5), row("old", 900 * HOUR)], exp(), NOW)
    d = rw.build_digest(s, NOW)
    assert d["severity"] == "warning"
    assert "gas_kitchen" in d["message"] and d["late"] == ["gas_kitchen"]
    assert d["dormant"] == ["old"]                             # reported, not alerted


# ── config ─────────────────────────────────────────────────────────────────────────────────────────
def test_shipped_expectations_file_loads_and_is_sane():
    e = rw.load_expectations()
    assert e.default_stale_s > 0
    assert e.dormant_after_s > e.default_stale_s, "dormant must be looser than the alert threshold"


if __name__ == "__main__":
    from tests._harness import run_module
    raise SystemExit(run_module(globals()))
