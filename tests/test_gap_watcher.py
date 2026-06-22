"""Tests for the gap watcher's pure gap-detection + routing (tools/gap_watcher.py)."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import gap_watcher as G  # noqa: E402  (also puts repo root on path → server.mesh importable)
from server.mesh import store as MS  # noqa: E402
from server.mesh.topology import SERVER  # noqa: E402
from tests._harness import run_module  # noqa: E402

_PRO = {"device_id": "meter_pro_x", "device_type": "switchbot_meter_pro", "area": "c_office"}


def _graph_db():
    c = sqlite3.connect(":memory:")
    MS.ensure_schema(c)
    return c


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


# ── mesh-aware chooser (choose_plan) ─────────────────────────────────────────────
def test_choose_plan_no_conn_is_static():
    assert G.choose_plan(_PRO, None, None) == G.backfill_plan(_PRO, None)


def test_choose_plan_empty_graph_falls_back_to_static():
    c = _graph_db()                                  # schema exists but no links
    assert G.choose_plan(_PRO, None, c) == G.backfill_plan(_PRO, None)


def test_choose_plan_direct_server_when_only_host_hears_it():
    c = _graph_db()
    MS.record_link(c, SERVER, ("endpoint", "meter_pro_x"), "ble-adv", rssi=-60)
    plan = G.choose_plan(_PRO, None, c)
    assert plan["via"] == "server" and plan["graph_path"]


def test_choose_plan_one_hop_picks_edge_node():
    c = _graph_db()
    MS.record_link(c, SERVER, ("node", "c6-bench"), "ip", ok=True)
    MS.record_link(c, ("node", "c6-bench"), ("endpoint", "meter_pro_x"), "ble-adv", rssi=-65)
    plan = G.choose_plan(_PRO, None, c)
    assert plan["via"] == "edge" and plan["node"] == "c6-bench" and plan["profile"] == "meter_pro"


def test_choose_plan_multihop_falls_back_to_static():
    c = _graph_db()
    MS.record_link(c, SERVER, ("node", "a"), "ip", ok=True)
    MS.record_link(c, ("node", "a"), ("node", "b"), "espnow", rssi=-60)
    MS.record_link(c, ("node", "b"), ("endpoint", "meter_pro_x"), "ble-adv", rssi=-70)
    plan = G.choose_plan(_PRO, None, c)              # 2-hop path exists, transport not built
    assert plan == G.backfill_plan(_PRO, None)       # → static fallback


def test_choose_plan_pull_failure_steers_away_from_host():
    # host hears it loud but has only failed; node hears it weaker but succeeded → pick the node
    c = _graph_db()
    MS.record_link(c, SERVER, ("endpoint", "meter_pro_x"), "ble-adv", rssi=-55)
    MS.record_link(c, SERVER, ("node", "c6-bench"), "ip", ok=True)
    MS.record_link(c, ("node", "c6-bench"), ("endpoint", "meter_pro_x"), "ble-adv", rssi=-75)
    MS.record_pull(c, "meter_pro_x", "server:server>meter_pro_x", ok=False, reason="no metadata")
    MS.record_pull(c, "meter_pro_x", "server:server>node:c6-bench>meter_pro_x", ok=True, n_samples=100)
    plan = G.choose_plan(_PRO, None, c)
    assert plan["via"] == "edge" and plan["node"] == "c6-bench"


if __name__ == "__main__":
    run_module(globals())
