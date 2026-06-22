"""Tests for the pure mesh graph + pathfinder (server/mesh/topology.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.mesh import topology as T  # noqa: E402
from server.mesh.topology import Link, build_graph, best_path, hops, serialize, SERVER  # noqa: E402
from tests._harness import run_module  # noqa: E402

EP = ("endpoint", "meter_pro_x")
NODE_A = ("node", "c6-bench")
NODE_B = ("node", "c6-attic")


def test_direct_server_ble():
    g = build_graph([Link(SERVER, EP, "ble-adv", rssi=-60)])
    path, cost = best_path(g, EP)
    assert path == [SERVER, EP]
    assert hops(path) == 0
    assert cost < T.UNREACHABLE


def test_one_relay_hop():
    g = build_graph([Link(SERVER, NODE_A, "ip"), Link(NODE_A, EP, "ble-adv", rssi=-65)])
    path, _ = best_path(g, EP)
    assert path == [SERVER, NODE_A, EP]
    assert hops(path) == 1


def test_prefers_stronger_rssi_route():
    # host hears EP weakly (-90); a node hears it strongly (-55) and has IP backhaul
    g = build_graph([
        Link(SERVER, EP, "ble-adv", rssi=-90),
        Link(SERVER, NODE_A, "ip"),
        Link(NODE_A, EP, "ble-adv", rssi=-55),
    ])
    path, _ = best_path(g, EP)
    assert path == [SERVER, NODE_A, EP]   # via the node, not the weak direct link


def test_pull_history_overrides_loudness():
    # THE A8:02 CASE: server hears EP loudest (-55) but has only FAILED to pull it;
    # a node hears it weaker (-75) but has SUCCEEDED. The proven puller must win.
    g = build_graph([
        Link(SERVER, EP, "ble-adv", rssi=-55),
        Link(SERVER, NODE_A, "ip"),
        Link(NODE_A, EP, "ble-adv", rssi=-75),
    ])
    stats = {("server", "meter_pro_x"): (0, 3), ("c6-bench", "meter_pro_x"): (4, 0)}
    path, _ = best_path(g, EP, pull_stats=stats)
    assert path == [SERVER, NODE_A, EP]
    # without the history, loudness wins (direct)
    path2, _ = best_path(g, EP)
    assert path2 == [SERVER, EP]


def test_multi_hop_chain():
    # server -> node_a (ip) -> node_b (espnow) -> endpoint (ble): the no-wired-backhaul case
    g = build_graph([
        Link(SERVER, NODE_A, "ip"),
        Link(NODE_A, NODE_B, "espnow", rssi=-60),
        Link(NODE_B, EP, "ble-adv", rssi=-70),
    ])
    path, cost = best_path(g, EP)
    assert path == [SERVER, NODE_A, NODE_B, EP]
    assert hops(path) == 2                 # dispatcher: needs the multi-hop relay transport
    assert cost < T.UNREACHABLE


def test_unreachable():
    g = build_graph([Link(SERVER, NODE_A, "ip")])   # node reachable, but nothing hears EP
    path, cost = best_path(g, EP)
    assert path is None and cost == T.UNREACHABLE


def test_link_cost_monotonic_in_rssi():
    strong = T.link_cost(Link(SERVER, EP, "ble-adv", rssi=-50))
    weak = T.link_cost(Link(SERVER, EP, "ble-adv", rssi=-95))
    assert weak > strong


def test_failing_link_is_penalised():
    clean = T.link_cost(Link(SERVER, EP, "ble-adv", rssi=-60, n_ok=10, n_fail=0))
    flaky = T.link_cost(Link(SERVER, EP, "ble-adv", rssi=-60, n_ok=0, n_fail=10))
    assert flaky > clean


def test_serialize_and_hops():
    path = [SERVER, NODE_A, EP]
    assert serialize(path) == "server:server>node:c6-bench>meter_pro_x"
    assert hops([SERVER, EP]) == 0


if __name__ == "__main__":
    run_module(globals())
