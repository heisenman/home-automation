"""tools/erv_min.py: start saves the ERV's bands + dry-boost level and sets level 1; a re-run keeps the ORIGINAL
saved values; stop restores them and removes the marker."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BANDS = [{"max": 60, "level": 3}, {"max": 75, "level": 2}, {"max": None, "level": 1}]


def load(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("erv_min", ROOT / "tools" / "erv_min.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.MARKER = tmp_path / "marker.json"
    pol = {"control": {"bands": list(BANDS)}, "dry_boost": {"outdoor_sensor": "x"}}

    def api(method, path, body=None):
        if method == "GET":
            return 200, {"policy": json.loads(json.dumps(pol))}
        for k, v in body.items():
            pol[k].update(v)
        return 200, {"status": "ok"}
    monkeypatch.setattr(m, "api", api)
    return m, pol


def test_start_then_stop_restores(tmp_path, monkeypatch, capsys):
    m, pol = load(tmp_path, monkeypatch)
    m.start()
    assert pol["control"]["bands"] == [{"max": None, "level": 1}] and pol["dry_boost"]["level"] == 1
    m.start()                                        # retry: must not save the minimum as "previous"
    assert json.loads(m.MARKER.read_text())["bands"] == BANDS
    m.stop()
    assert pol["control"]["bands"] == BANDS and pol["dry_boost"]["level"] == 3 and not m.MARKER.exists()


def test_stop_without_marker_does_nothing(tmp_path, monkeypatch, capsys):
    m, pol = load(tmp_path, monkeypatch)
    m.stop()
    assert pol["control"]["bands"] == BANDS and "level" not in pol["dry_boost"]
