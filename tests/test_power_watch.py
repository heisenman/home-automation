"""Tests for the power regression watch (server/maintenance/power_watch.py, ADR-0039).

Drives the pure core — no DB, no API, no bus. The behaviours worth pinning:

  * drift is judged on a WINDOW MEAN with consecutive-check hysteresis, so a compile can't trip it
  * a meter that stops reporting is an ALARM, not silence (the ADR-0032 23h silent-drop mode)
  * `window_means` is the statistic baselines are characterized from, so its bucketing has to be right
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.maintenance import power_watch as pw  # noqa: E402

NOW = 1_000_000.0
HOUR = 3600.0


def meter(**kw):
    base = dict(device_id="m1", baseline_w=8.0, tolerance_w=2.0, window_h=6.0,
                stale_after_h=2.0, consecutive=2)
    base.update(kw)
    return pw.Meter(**base)


def samples_at(watts, *, hours_back_from=5.5, count=60, now=NOW):
    """`count` evenly spaced samples inside the window, all at `watts`."""
    step = (hours_back_from * HOUR) / max(1, count)
    return [(now - hours_back_from * HOUR + i * step, watts) for i in range(count)]


def kinds(v):
    return [a["kind"] for a in v.alerts]


# ── the healthy path ───────────────────────────────────────────────────────────────────────────────
def test_at_baseline_is_quiet():
    v = pw.evaluate(meter(), samples_at(8.0), {}, NOW)
    assert v.status == "ok" and v.alerts == []
    assert abs(v.mean_w - 8.0) < 1e-9


def test_under_ceiling_is_quiet():
    """Baseline 8.0 + tolerance 2.0 => 10.0 ceiling. 9.9 must not alert."""
    v = pw.evaluate(meter(), samples_at(9.9), {}, NOW)
    assert v.status == "ok" and v.alerts == []


# ── drift ──────────────────────────────────────────────────────────────────────────────────────────
def test_single_breach_is_held_by_hysteresis():
    s = {}
    v = pw.evaluate(meter(), samples_at(12.0), s, NOW)
    assert v.status == "ok" and v.alerts == []
    assert s["breaches"] == 1


def test_sustained_breach_alerts():
    s = {}
    pw.evaluate(meter(), samples_at(12.0), s, NOW)
    v = pw.evaluate(meter(), samples_at(12.0), s, NOW)
    assert v.status == "high"
    assert "power_regression" in kinds(v)
    a = v.alerts[0]
    assert a["mean_w"] == 12.0 and a["baseline_w"] == 8.0


def test_regression_alert_fires_once_not_every_check():
    s = {}
    for _ in range(2):
        pw.evaluate(meter(), samples_at(12.0), s, NOW)
    v = pw.evaluate(meter(), samples_at(12.0), s, NOW)
    assert v.status == "high" and kinds(v) == []


def test_breach_counter_resets_on_a_good_check():
    """Two NON-consecutive breaches must not add up to an alert."""
    s = {}
    pw.evaluate(meter(), samples_at(12.0), s, NOW)
    pw.evaluate(meter(), samples_at(8.0), s, NOW)
    v = pw.evaluate(meter(), samples_at(12.0), s, NOW)
    assert v.status == "ok" and kinds(v) == []


def test_recovery_alerts_once():
    s = {}
    for _ in range(2):
        pw.evaluate(meter(), samples_at(12.0), s, NOW)
    v = pw.evaluate(meter(), samples_at(8.0), s, NOW)
    assert v.status == "ok" and "power_normal" in kinds(v)
    v2 = pw.evaluate(meter(), samples_at(8.0), s, NOW)
    assert kinds(v2) == []


def test_consecutive_is_configurable():
    s = {}
    m = meter(consecutive=3)
    for _ in range(2):
        assert pw.evaluate(m, samples_at(12.0), s, NOW).status == "ok"
    assert pw.evaluate(m, samples_at(12.0), s, NOW).status == "high"


# ── the window is a window ─────────────────────────────────────────────────────────────────────────
def test_samples_outside_the_window_are_ignored():
    """An old spike must not keep alerting once it has aged out."""
    old_spike = [(NOW - 20 * HOUR, 50.0)]
    v = pw.evaluate(meter(), old_spike + samples_at(8.0), {}, NOW)
    assert v.status == "ok"
    assert abs(v.mean_w - 8.0) < 1e-9


def test_mean_not_max_decides():
    """One 30s spike inside an otherwise normal window must not alert — that's a compile, not a regression."""
    s = samples_at(8.0, count=200) + [(NOW - HOUR, 60.0)]
    v = pw.evaluate(meter(), s, {}, NOW)
    assert v.status == "ok"


# ── absence is its own alarm (ADR-0032) ────────────────────────────────────────────────────────────
def test_meter_gone_quiet_alerts_stale():
    s = {}
    stale = [(NOW - 30 * HOUR, 8.0)]                    # last heard 30h ago
    v = pw.evaluate(meter(), stale, s, NOW)
    assert v.status == "stale"
    assert "power_meter_stale" in kinds(v)


def test_never_reported_alerts_stale():
    v = pw.evaluate(meter(), [], {}, NOW)
    assert v.status == "stale" and "power_meter_stale" in kinds(v)


def test_stale_alert_fires_once():
    s = {}
    stale = [(NOW - 30 * HOUR, 8.0)]
    pw.evaluate(meter(), stale, s, NOW)
    v = pw.evaluate(meter(), stale, s, NOW)
    assert v.status == "stale" and kinds(v) == []


def test_meter_returning_clears_stale():
    s = {}
    pw.evaluate(meter(), [(NOW - 30 * HOUR, 8.0)], s, NOW)
    v = pw.evaluate(meter(), samples_at(8.0), s, NOW)
    assert v.status == "ok" and "power_meter_ok" in kinds(v)


def test_recent_gap_shorter_than_stale_after_is_not_an_alarm():
    """A meter last heard 1h ago with stale_after_h=2 is late, not dead — don't cry wolf."""
    v = pw.evaluate(meter(), [(NOW - 1 * HOUR, 8.0)], {}, NOW)
    # inside the 6h window, so it evaluates normally rather than going stale
    assert v.status == "ok"


def test_stale_beats_drift():
    """No data must never be scored as a power verdict."""
    v = pw.evaluate(meter(), [(NOW - 50 * HOUR, 99.0)], {}, NOW)
    assert v.status == "stale" and v.mean_w is None


# ── window_means: the statistic baselines are characterized from ───────────────────────────────────
def test_window_means_buckets_by_window():
    # 3 full 1h buckets at 1/2/3 W, plus a partial 4th that must be dropped
    s = ([(NOW + i * 60, 1.0) for i in range(60)]
         + [(NOW + HOUR + i * 60, 2.0) for i in range(60)]
         + [(NOW + 2 * HOUR + i * 60, 3.0) for i in range(60)]
         + [(NOW + 3 * HOUR, 99.0)])
    assert pw.window_means(s, 1.0) == [1.0, 2.0, 3.0]


def test_window_means_drops_partial_trailing_bucket():
    """A half-full window's mean isn't comparable to a full one, so it must not skew a baseline."""
    s = [(NOW + i * 60, 5.0) for i in range(60)] + [(NOW + HOUR, 100.0)]
    assert pw.window_means(s, 1.0) == [5.0]


def test_window_means_empty_and_degenerate():
    assert pw.window_means([], 6.0) == []
    assert pw.window_means([(NOW, 5.0)], 0) == []


def test_window_means_is_order_insensitive():
    s = [(NOW + 2 * HOUR, 3.0), (NOW, 1.0), (NOW + HOUR, 2.0), (NOW + 3 * HOUR, 9.0)]
    assert pw.window_means(s, 1.0) == [1.0, 2.0, 3.0]


# ── config ─────────────────────────────────────────────────────────────────────────────────────────
def test_shipped_baselines_load_and_are_sane():
    """The committed file must parse, and every ceiling must sit above its baseline."""
    meters = pw.load_meters()
    assert meters, "provisioning/power-baselines.yaml defines no meters"
    for m in meters:
        assert m.baseline_w > 0, f"{m.device_id}: non-positive baseline"
        assert m.tolerance_w > 0, f"{m.device_id}: non-positive tolerance"
        assert m.ceiling_w > m.baseline_w
        assert m.consecutive >= 1 and m.window_h > 0


def test_defaults_are_applied_and_overridable():
    import tempfile
    import textwrap
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(textwrap.dedent("""
            defaults: {window_h: 6, tolerance_w: 2.0, consecutive: 2}
            meters:
              - {device_id: a, baseline_w: 5}
              - {device_id: b, baseline_w: 5, tolerance_w: 9, consecutive: 4}
        """))
        p = Path(f.name)
    a, b = pw.load_meters(p)
    assert (a.tolerance_w, a.consecutive) == (2.0, 2)
    assert (b.tolerance_w, b.consecutive) == (9.0, 4)
    p.unlink()


# ── source selection: "has rows" is not "has the window" ───────────────────────────────────────────
# hot.db is pruned daily, so on ha-2 it holds ~today-so-far. Preferring it on mere non-emptiness would
# evaluate a 6h window against 2h of data and characterize a 7d baseline from an afternoon.
def _patch(monkey: dict):
    """Swap the two readers for canned results; returns a restore callable."""
    orig = (pw.read_sqlite, pw.read_api)
    pw.read_sqlite = monkey["sqlite"]
    pw.read_api = monkey["api"]
    return lambda: (setattr(pw, "read_sqlite", orig[0]), setattr(pw, "read_api", orig[1]))


def test_sqlite_used_when_it_covers_the_window():
    restore = _patch({"sqlite": lambda d, s: [(s, 1.0), (s + 10, 1.0)],
                      "api": lambda d, s: [(s, 9.9)]})
    try:
        rows, src = pw.read_samples("m1", NOW - 6 * HOUR)
        assert src == "sqlite" and rows[0][1] == 1.0
    finally:
        restore()


def test_api_used_when_local_db_is_short():
    """The local DB has data, just not far enough back — must NOT be silently preferred."""
    since = NOW - 7 * 86400
    restore = _patch({"sqlite": lambda d, s: [(NOW - HOUR, 1.0)],          # only the last hour
                      "api": lambda d, s: [(s, 9.9), (NOW, 9.9)]})
    try:
        rows, src = pw.read_samples("m1", since)
        assert src.startswith("api"), f"expected api, got {src}"
        assert "short by" in src                                          # and it says how short
        assert rows[0][1] == 9.9
    finally:
        restore()


def test_partial_local_db_used_only_when_api_is_also_down():
    """Degrade to partial history rather than no verdict — but the source must say so."""
    since = NOW - 7 * 86400
    restore = _patch({"sqlite": lambda d, s: [(NOW - HOUR, 1.0)], "api": lambda d, s: []})
    try:
        rows, src = pw.read_samples("m1", since)
        assert src == "sqlite(partial)" and rows
    finally:
        restore()


def test_empty_local_db_falls_straight_to_api():
    restore = _patch({"sqlite": lambda d, s: [], "api": lambda d, s: [(s, 5.0)]})
    try:
        rows, src = pw.read_samples("m1", NOW - 6 * HOUR)
        assert src == "api" and rows[0][1] == 5.0
    finally:
        restore()


if __name__ == "__main__":
    from tests._harness import run_module
    raise SystemExit(run_module(globals()))
