"""server/maintenance/device_placement.py + the PUT /placement handler (map arc 3, spatial room-zoom).

The placement write path: upsert device -> {x,y,anchor} in device-placement.yaml, comment/alignment
preserving + atomic; and the endpoint's validation (x,y in [0,1] or null, anchor in n|s|e|w|auto).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.maintenance.device_placement import write_placement  # noqa: E402
from server.api.control import handle_device_placement           # noqa: E402

_SEED = ("placements:\n"
         "  meter_attic:        { x: null, y: null, anchor: auto }   # attic · sensor · outdoor\n"
         "  gas_c_bed:          { x: null, y: null, anchor: auto }   # c_bed · sensor · sgp30\n")


def _file(tmp_path):
    p = tmp_path / "device-placement.yaml"
    p.write_text(_SEED)
    return p


def test_write_updates_in_place_preserving_comment_and_alignment(tmp_path):
    p = _file(tmp_path)
    assert write_placement(p, "meter_attic", 0.42, 0.71, "e") == "updated"
    lines = p.read_text().splitlines()
    attic = next(ln for ln in lines if ln.strip().startswith("meter_attic:"))
    assert "{ x: 0.42, y: 0.71, anchor: e }" in attic
    assert "# attic · sensor · outdoor" in attic          # comment preserved
    assert attic.startswith("  meter_attic:        ")       # alignment preserved
    # the other device is untouched
    assert any("gas_c_bed:          { x: null, y: null, anchor: auto }" in ln for ln in lines)


def test_write_null_clears_a_placement(tmp_path):
    p = _file(tmp_path)
    write_placement(p, "meter_attic", 0.5, 0.5, "n")
    assert write_placement(p, "meter_attic", None, None, "auto") == "updated"
    assert any("meter_attic:" in ln and "{ x: null, y: null, anchor: auto }" in ln
               for ln in p.read_text().splitlines())


def test_write_appends_unknown_device(tmp_path):
    p = _file(tmp_path)
    assert write_placement(p, "new_dev", 0.1, 0.9, "s") == "appended"
    assert any(ln.strip() == "new_dev: { x: 0.1, y: 0.9, anchor: s }" for ln in p.read_text().splitlines())


def test_handler_validates_range_and_anchor(tmp_path):
    p = _file(tmp_path)
    assert handle_device_placement("meter_attic", {"x": 1.5, "y": 0.5}, p)[0] == 400   # x out of [0,1]
    assert handle_device_placement("meter_attic", {"x": -0.1, "y": 0.5}, p)[0] == 400
    assert handle_device_placement("meter_attic", {"x": 0.5, "y": 0.5, "anchor": "z"}, p)[0] == 400
    assert handle_device_placement("meter_attic", {"x": True, "y": 0.5}, p)[0] == 400   # bool is not a coord
    code, payload = handle_device_placement("meter_attic", {"x": 0.3, "y": 0.6, "anchor": "w"}, p)
    assert code == 200 and payload["placement"] == {"x": 0.3, "y": 0.6, "anchor": "w"}
    # null coords are valid (clears placement)
    assert handle_device_placement("meter_attic", {"x": None, "y": None}, p)[0] == 200


if __name__ == "__main__":
    from tests._harness import run_module
    raise SystemExit(run_module(globals()))
