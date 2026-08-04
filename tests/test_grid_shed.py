"""Voluntary load-shed decision law + sources (server/grid/, ADR-0037).

The decision law is where the safety lives, so it is tested without a network or a controller.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.grid import shed as law  # noqa: E402
from server.grid.sources import ScheduleSource, _in_season, _local_window, build_source  # noqa: E402
from tests._harness import run_module  # noqa: E402

NOW = 1_800_000_000.0
CEILING = 55.0
WIN = law.ShedWindow(NOW - 600, NOW + 3600, "test window", "schedule")


def _d(rh=45.0, window=WIN, live=None, ours=None, now=NOW, ceiling=CEILING):
    return law.decide(now, window, rh, ceiling, live, ours)


# ── the happy path ────────────────────────────────────────────────────────────────
def test_sheds_inside_window_when_dry_enough():
    d = _d()
    assert d.action == law.SHED
    assert 59 < d.duration_min < 61            # sized to the window end, not a fixed length


def test_shed_duration_always_ends_at_the_window_end():
    """The TTL is the safety net: if this lane dies mid-event the override still expires on time."""
    d = _d(now=NOW + 1800)
    assert d.action == law.SHED and 29 < d.duration_min < 31


def test_no_window_no_shed():
    assert _d(window=None).action == law.HOLD


def test_outside_the_window_is_a_hold_not_a_shed():
    assert _d(now=NOW + 7200).action == law.HOLD


# ── the guardrail Hugh asked for ──────────────────────────────────────────────────
def test_rh_at_ceiling_vetoes_the_shed():
    d = _d(rh=CEILING)
    assert d.action == law.HOLD and "vetoed" in d.reason


def test_rh_climbing_mid_shed_aborts_it():
    """The veto is not just an entry check — a shed already running is released when RH crosses."""
    d = _d(rh=57.0, live=NOW + 3600, ours=NOW + 3600)
    assert d.action == law.RELEASE and "aborting" in d.reason


def test_missing_humidity_forbids_the_shed():
    """An unverifiable guardrail must not permit the shed — failing toward comfort, not savings."""
    assert _d(rh=None).action == law.HOLD


def test_a_device_opted_out_of_the_guardrail_sheds_on_the_window_alone():
    d = _d(rh=None, ceiling=None)
    assert d.action == law.SHED and "no guardrail" in d.reason


def test_guardrail_is_per_device_metric_not_a_global_rh():
    """The purifier's guardrail is PM2.5, not RH. A global RH ceiling would be permanently unverifiable
    for it — it reports no humidity — so it would silently never shed while the lane looked healthy."""
    from server.grid.__main__ import parse_guardrails
    g = parse_guardrails("dehumidifier_living_room:55,purifier_living_room:35")
    assert g == {"dehumidifier_living_room": 55.0, "purifier_living_room": 35.0}
    # PM2.5 35 ug/m3 (smoke) vetoes the purifier shed on exactly the same law that RH 55 vetoes the dehum
    assert law.decide(NOW, WIN, 40.0, g["purifier_living_room"], None, None).action == law.HOLD
    assert law.decide(NOW, WIN, 20.0, g["purifier_living_room"], None, None).action == law.SHED


def test_guardrail_none_opts_a_device_out_explicitly():
    from server.grid.__main__ import parse_guardrails
    assert parse_guardrails("purifier_living_room:none") == {"purifier_living_room": None}


def test_bad_guardrail_spec_is_rejected():
    from server.grid.__main__ import parse_guardrails
    try:
        parse_guardrails("dehumidifier_living_room")
        assert False, "a ceiling-less entry should be rejected"
    except SystemExit as e:
        assert "want" in str(e)


def test_missing_humidity_releases_a_running_shed():
    d = _d(rh=None, live=NOW + 3600, ours=NOW + 3600)
    assert d.action == law.RELEASE and "cannot verify" in d.reason


# ── never fight a human ───────────────────────────────────────────────────────────
def test_a_human_override_is_never_touched():
    """Someone paused the device themselves — an automatic curtailment must not overwrite it."""
    d = _d(live=NOW + 99999, ours=None)
    assert d.action == law.HOLD and "manual override" in d.reason


def test_a_human_override_is_not_released_either():
    """Even out of window: releasing someone's deliberate pause would be just as wrong as clobbering it."""
    d = _d(window=None, live=NOW + 99999, ours=None)
    assert d.action == law.HOLD


def test_our_own_override_is_recognised_despite_clock_drift():
    """Expiry is the only marker available — the control API stores (action, expiry) and nothing else."""
    d = _d(window=None, live=NOW + 3600, ours=NOW + 3600 + 45)
    assert d.action == law.RELEASE


def test_a_human_override_landing_far_from_ours_is_left_alone():
    d = _d(window=None, live=NOW + 3600, ours=NOW + 3600 + 600)
    assert d.action == law.HOLD


# ── release / cleanup ─────────────────────────────────────────────────────────────
def test_window_end_releases_our_shed():
    d = _d(window=None, live=NOW + 60, ours=NOW + 60)
    assert d.action == law.RELEASE and "window ended" in d.reason


def test_self_expired_override_is_forgotten():
    """The override ran out on its own (the .210-is-dead path). Nothing to clear, just drop the state."""
    d = _d(window=None, live=None, ours=NOW - 10)
    assert d.action == law.RELEASE and "already expired" in d.reason


def test_shed_is_reasserted_idempotently_while_running():
    d = _d(live=NOW + 3600, ours=NOW + 3600)
    assert d.action == law.SHED and "holding" in d.reason


# ── sources ───────────────────────────────────────────────────────────────────────
def test_schedule_source_only_fires_inside_its_window():
    s = ScheduleSource(window_spec="00:00-23:59", season=None)
    assert s.window(time.time()) is not None
    s2 = ScheduleSource(window_spec="03:00-03:01", season=None)
    w = s2.window(time.time())
    assert w is None or w.source == "schedule"        # only fires in that one minute


def test_season_bounds_wrap_the_new_year():
    jul = time.mktime((2026, 7, 15, 12, 0, 0, 0, 0, -1))
    jan = time.mktime((2026, 1, 15, 12, 0, 0, 0, 0, -1))
    assert _in_season(jul, "06-01..09-30") and not _in_season(jan, "06-01..09-30")
    assert _in_season(jan, "11-01..02-28") and not _in_season(jul, "11-01..02-28")


def test_window_spec_wrapping_midnight_lands_on_the_next_day():
    start, end = _local_window(time.time(), "22:00-02:00")
    assert end > start and 3.9 * 3600 < (end - start) < 4.1 * 3600


def test_unimplemented_sources_fail_loudly_rather_than_silently_doing_nothing():
    for kind in ("imap", "manual"):
        try:
            build_source(kind)
            assert False, f"{kind} should refuse to build"
        except SystemExit as e:
            assert "not implemented" in str(e)


def test_unknown_source_is_rejected():
    try:
        build_source("pge")
        assert False, "unknown source should refuse to build"
    except SystemExit as e:
        assert "unknown source" in str(e)


if __name__ == "__main__":
    run_module(globals())
