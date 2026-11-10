"""tools/erv_night.py: the off-night guard clears the override only after two low air-quality checks in a row."""
import importlib.util
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(tmp_path, monkeypatch, aqs):
    spec = importlib.util.spec_from_file_location("erv_night", ROOT / "tools" / "erv_night.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.MARKER = tmp_path / "marker.json"
    calls = []

    def api(method, path, body=None):
        calls.append(body)
        return 200, {"status": "ok", "override": body}
    monkeypatch.setattr(m, "api", api)
    it = iter(aqs)
    monkeypatch.setattr(m, "aq_index", lambda: (next(it), 4))
    return m, calls


def test_guard_needs_two_low_checks_in_a_row(tmp_path, monkeypatch, capsys):
    m, calls = load(tmp_path, monkeypatch, [55, 70, 58, 50])
    m.start("off", 540)
    assert calls[-1] == {"action": "off", "duration_min": 540}
    m.guard(); m.guard()                         # low, then recovered: the count resets
    assert json.loads(m.MARKER.read_text())["low_checks"] == 0
    m.guard(); m.guard()                         # low twice in a row: clear
    assert calls[-1] == {"action": "clear"} and not m.MARKER.exists()


def test_guard_ignores_high_nights_and_ends_with_the_night(tmp_path, monkeypatch, capsys):
    m, calls = load(tmp_path, monkeypatch, [10, 10])
    m.start("high", 540)
    assert calls[-1] == {"action": "boost_on", "duration_min": 540}
    m.guard(); m.guard()
    assert calls[-1]["action"] == "boost_on" and m.MARKER.exists()
    m.MARKER.write_text(json.dumps({"kind": "off", "end": time.time() - 1, "low_checks": 0}))
    m.guard()
    assert not m.MARKER.exists() and calls[-1]["action"] == "boost_on"
