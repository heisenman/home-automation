"""Tests for the gap watcher's pure gap-detection + routing (tools/gap_watcher.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import gap_watcher as G  # noqa: E402
from tests._harness import run_module  # noqa: E402


def test_no_gaps_when_dense():
    times = [i * 60.0 for i in range(60)]          # one reading/min for an hour
    assert G.find_gaps(times, min_gap_s=20 * 60) == []


def test_finds_a_gap():
    # readings then a 40-min hole then resume
    times = [0, 60, 120, 120 + 40 * 60, 120 + 41 * 60]
    gaps = G.find_gaps(times, min_gap_s=20 * 60)
    assert len(gaps) == 1
    start, end, dur = gaps[0]
    assert start == 120 and dur == 40 * 60


def test_threshold_excludes_small_gaps():
    times = [0, 60, 60 + 15 * 60, 60 + 16 * 60]    # a 15-min gap, below the 20-min threshold
    assert G.find_gaps(times, min_gap_s=20 * 60) == []


def test_multiple_gaps():
    times = [0, 30 * 60, 30 * 60 + 60, 90 * 60]    # two ~30/60-min gaps
    assert len(G.find_gaps(times, min_gap_s=20 * 60)) == 2


def test_route_explicit_wins():
    info = {"device_type": "switchbot_meter_outdoor", "backfill": {"via": "server", "profile": "outdoor"}}
    assert G.backfill_plan(info)["via"] == "server"


def test_route_file_overrides_inference():
    info = {"device_id": "meter_h_bed", "device_type": "switchbot_meter_outdoor"}  # would infer edge
    routes = {"meter_h_bed": {"via": "server"}}
    assert G.backfill_plan(info, routes)["via"] == "server"


def test_route_precedence_registry_beats_file():
    info = {"device_id": "x", "device_type": "switchbot_meter_pro", "backfill": {"via": "edge"}}
    assert G.backfill_plan(info, {"x": {"via": "server"}})["via"] == "edge"


def test_route_inferred_by_type():
    assert G.backfill_plan({"device_type": "switchbot_meter_outdoor"}) == {
        "via": "edge", "node": G.EDGE_NODE, "profile": "outdoor"}
    assert G.backfill_plan({"device_type": "switchbot_meter_pro"})["profile"] == "meter_pro"
    assert G.backfill_plan({"device_type": "aranet_radon"}) == {"via": "aranet"}
    assert G.backfill_plan({"device_type": "mystery_widget"}) is None


if __name__ == "__main__":
    run_module(globals())
