"""tools/dehum_block.py: start parks the Midea with a self-expiring override and saves/sets the Aprilaire thresholds;
stop restores them; a re-run start keeps the ORIGINAL saved thresholds (not the block's 44/40)."""
import importlib.util
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("dehum_block", ROOT / "tools" / "dehum_block.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.MARKER = tmp_path / "marker.json"
    state = {"apr": {"on_above": 48, "off_below": 45}, "override": None, "calls": []}

    def api(method, path, body=None):
        state["calls"].append((method, path, body))
        if method == "GET":
            return 200, {"policy": {"control": dict(state["apr"])}, "override": state["override"]}
        if path.endswith("/override"):
            state["override"] = None if body["action"] == "clear" else body
            return 200, {"status": "ok", "override": state["override"]}
        state["apr"].update(body["control"])
        return 200, {"status": "ok"}
    monkeypatch.setattr(m, "api", api)
    return m, state


def test_start_then_stop_restores(tmp_path, monkeypatch, capsys):
    m, s = load(tmp_path, monkeypatch)
    end = time.time() + 3600
    m.start(end)
    assert s["apr"] == {"on_above": 44, "off_below": 40}
    assert s["override"]["action"] == "off" and 115 <= s["override"]["duration_min"] <= 121   # 60 + guard 60
    m.start(end)                                     # retry: must not save 44/40 as "previous"
    assert json.loads(m.MARKER.read_text())["aprilaire"] == {"on_above": 48, "off_below": 45}
    m.stop()
    assert s["apr"] == {"on_above": 48, "off_below": 45} and s["override"] is None and not m.MARKER.exists()


def test_start_after_end_does_nothing(tmp_path, monkeypatch, capsys):
    m, s = load(tmp_path, monkeypatch)
    m.start(time.time() - 1)
    assert s["calls"] == [] and not m.MARKER.exists()
