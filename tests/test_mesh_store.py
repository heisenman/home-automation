"""Tests for the mesh store DB layer (server/mesh/store.py) using in-memory sqlite."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.mesh import store as S  # noqa: E402
from server.mesh.topology import SERVER, best_path, build_graph  # noqa: E402
from tests._harness import run_module  # noqa: E402

NODE = ("node", "c6-bench")
EP = ("endpoint", "meter_pro_x")


def _db():
    c = sqlite3.connect(":memory:")
    S.ensure_schema(c)
    return c


def test_record_link_upsert_and_counters():
    c = _db()
    S.record_link(c, SERVER, NODE, "ip", ok=True)
    S.record_link(c, SERVER, NODE, "ip", ok=True)
    S.record_link(c, SERVER, NODE, "ip", ok=False)
    row = c.execute("SELECT n_ok, n_fail FROM mesh_links WHERE link_kind='ip'").fetchone()
    assert row == (2, 1)                       # upserted in place, counters accumulated
    assert c.execute("SELECT count(*) FROM mesh_links").fetchone()[0] == 1


def test_record_link_passive_sighting_refreshes_rssi():
    c = _db()
    S.record_link(c, NODE, EP, "ble-adv", rssi=-80, ok=None)
    S.record_link(c, NODE, EP, "ble-adv", rssi=-60, ok=None)   # newer, louder
    r = c.execute("SELECT rssi, n_ok, n_fail FROM mesh_links WHERE dst_id='meter_pro_x'").fetchone()
    assert r == (-60, 0, 0)


def test_load_links_roundtrips_into_graph():
    c = _db()
    S.record_link(c, SERVER, NODE, "ip", ok=True)
    S.record_link(c, NODE, EP, "ble-adv", rssi=-65, ok=None)
    g = build_graph(S.load_links(c))
    path, _ = best_path(g, EP)
    assert path == [SERVER, NODE, EP]


def test_pull_stats_aggregation_by_terminal_receiver():
    c = _db()
    S.record_pull(c, "meter_pro_x", "server:server>node:c6-bench>meter_pro_x", ok=True, n_samples=120)
    S.record_pull(c, "meter_pro_x", "server:server>node:c6-bench>meter_pro_x", ok=True, n_samples=90)
    S.record_pull(c, "meter_pro_x", "server:server>meter_pro_x", ok=False, reason="no metadata")
    stats = S.pull_stats(c)
    assert stats[("c6-bench", "meter_pro_x")] == (2, 0)
    assert stats[("server", "meter_pro_x")] == (0, 1)


def test_terminal_receiver_parsing():
    assert S._terminal_receiver("server:server>node:c6-bench>meter_pro_x") == "c6-bench"
    assert S._terminal_receiver("server:server>meter_pro_x") == "server"
    assert S._terminal_receiver("c6-bench") == "c6-bench"
    assert S._terminal_receiver(None) is None


def test_pull_stats_feeds_routing_override():
    # end-to-end: a failed-direct + succeeded-via-node history makes the chooser pick the node
    c = _db()
    S.record_link(c, SERVER, EP, "ble-adv", rssi=-55)          # loud direct
    S.record_link(c, SERVER, NODE, "ip", ok=True)
    S.record_link(c, NODE, EP, "ble-adv", rssi=-75)            # quieter via node
    S.record_pull(c, "meter_pro_x", "server:server>meter_pro_x", ok=False, reason="no metadata")
    S.record_pull(c, "meter_pro_x", "server:server>node:c6-bench>meter_pro_x", ok=True, n_samples=100)
    g = build_graph(S.load_links(c))
    path, _ = best_path(g, EP, pull_stats=S.pull_stats(c))
    assert path == [SERVER, NODE, EP]


if __name__ == "__main__":
    run_module(globals())
