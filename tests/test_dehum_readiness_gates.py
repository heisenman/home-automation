"""Study readiness SE gates (Hugh 2026-11-06): a target, hysteresis once passed, and a preponderance-of-data path."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import dehum_readiness as R  # noqa: E402


def test_target_hysteresis_and_preponderance(monkeypatch):
    monkeypatch.setattr(R, "PREV_OK", {})
    assert R.se_gate("hvac_zero", 0.049, 0.05)[0] is True
    assert R.se_gate("hvac_zero", 0.060, 0.05)[0] is False
    monkeypatch.setattr(R, "PREV_OK", {"hvac_zero": True})
    ok, how = R.se_gate("hvac_zero", 0.060, 0.05)
    assert ok and "held" in how                                   # passed before: noise can't flip it back
    assert R.se_gate("hvac_zero", 0.070, 0.05)[0] is False        # beyond 1.3× target: it does re-open
    monkeypatch.setattr(R, "PREV_OK", {})
    ok, how = R.se_gate("hvac_zero", 0.08, 0.05, "8 h, no drift")
    assert ok and "preponderance" in how
    assert R.se_gate("hvac_zero", None, 0.05, "x")[0] is False    # no estimate: nothing to pass on


def test_day_drift_flags_a_step_between_days():
    day = 86400
    steady = [(d * day + i * 3600, 30, v) for d in range(4) for i, v in enumerate((0.01, -0.01, 0.02))]
    stepped = [(d * day + i * 3600, 30, v + (0.5 if d == 3 else 0)) for d in range(4)
               for i, v in enumerate((0.01, -0.01, 0.02))]
    assert R.day_drift(steady, 0.05)[1] > 0.05
    assert R.day_drift(stepped, 0.05)[1] < 0.05
    assert R.day_drift(steady[:3], 0.05) is None                  # one day: no test


def test_segments_split_long_calls():
    """G2b scores calls over 4 h per 4-h segment (Hugh 2026-11-09); a tail under 1 h joins the segment before."""
    h = 3600
    assert R.segments([(0, 3 * h)]) == [(0, 3 * h, True)]
    assert R.segments([(0, 9 * h)]) == [(0, 4 * h, True), (4 * h, 8 * h, False), (8 * h, 9 * h, False)]
    assert R.segments([(0, 8 * h + 1800)]) == [(0, 4 * h, True), (4 * h, 8 * h + 1800, False)]


def test_completed_gate_stays_complete(monkeypatch):
    """Hugh 2026-11-10: a gate once filed complete stays complete when its evidence ages out of the rolling window."""
    ok, bad = {"ok": True}, {"ok": False}
    monkeypatch.setattr(R, "PREV_DONE", {})
    assert R.gate_done("G2a", [ok, ok]) == (True, None)
    assert R.gate_done("G2a", [ok, bad]) == (False, None)
    monkeypatch.setattr(R, "PREV_DONE", {"G2a": "2026-11-05"})
    assert R.gate_done("G2a", [ok, bad]) == (True, "2026-11-05")
    assert R.gate_done("G2a", [ok, ok]) == (True, None)           # met again today: nothing held
    assert R.gate_done("G2b", [ok, bad]) == (False, None)


def test_held_zero_offset_is_remeasured_from_recent_fan_only_windows():
    # 4 recent fan-only windows of 20 min (the rotation's zero windows), true offset +0.25; the held fit said +0.15
    zm = {"beta": [0.15, 0, 0.05, 0, 0.3, 0, 0.0], "se": [0.07, None, 0.01, None, 0.04, None, None],
          "hours": {"S1f": 28.9}}
    delta, state, x, cfm, tret = {}, {}, {}, {}, {}
    for w in range(4):
        for i in range(20):
            k = 1_000_000 + w * 5400 + i * 60
            x[k], cfm[k], tret[k] = 1.0, 66.0, R.T_REF
            delta[k] = 0.25 + 0.05 * 66 * 1.0 / 100 + (0.01 if i % 2 else -0.01)
            state[k] = "S1f"
    rec = R.zero_recent(delta, state, x, cfm, tret, zm, 0)
    assert rec["n"] == 4 and rec["minutes"] == 80 and abs(rec["offset"] - 0.25) < 1e-9
    assert R.zero_recent(delta, state, x, cfm, tret, zm, 1_000_000 + 2 * 5400) is None    # 2 windows: too few
    zr = {**zm, "beta": [0.25] + zm["beta"][1:], "se": [rec["se"]] + zm["se"][1:], "held": True,
          "remeasured": {**rec, "held_offset": 0.15, "held_se": 0.07}}
    st = R.stored_zero(zr)
    assert st["beta"][0] == 0.15 and st["se"][0] == 0.07 and "remeasured" not in st and "held" not in st
