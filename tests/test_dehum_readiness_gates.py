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
