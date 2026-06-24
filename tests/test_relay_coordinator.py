"""ADR-0015 Phase B — coordinator allowlist computation."""
from server.mesh.coordinator import build_directive, compute_allowlists
from server.mesh.topology import SERVER, Link


def test_edge_only_device_goes_to_that_node_local_device_stays_local():
    links = [
        # device X: only c6 hears it -> c6 is the preferred source
        Link(SERVER, ("node", "c6"), "ip"),
        Link(("node", "c6"), ("endpoint", "X"), "ble-adv", rssi=-60),
        # device Y: dictator's own radio hears it strongly -> local preferred (no edge directive)
        Link(SERVER, ("endpoint", "Y"), "ble-adv", rssi=-55),
    ]
    per_node, local_devs = compute_allowlists(links, {"X": "AA:AA", "Y": "BB:BB"})
    assert local_devs == ["Y"]
    assert per_node["c6"]["device_ids"] == ["X"]
    assert per_node["c6"]["relay_macs"] == ["AA:AA"]


def test_local_wins_ties_so_edge_allowlist_empty():
    # both hear X at equal rssi -> local wins (saves the IP hop) -> no edge directive at all.
    links = [
        Link(SERVER, ("endpoint", "X"), "ble-adv", rssi=-70),
        Link(SERVER, ("node", "c6"), "ip"),
        Link(("node", "c6"), ("endpoint", "X"), "ble-adv", rssi=-70),
    ]
    per_node, local_devs = compute_allowlists(links, {"X": "AA:AA"})
    assert local_devs == ["X"] and per_node == {}


def test_coordinator_injects_backhaul_so_a_fresh_edge_wins():
    # mesh.db-style input: ONLY receiver->endpoint reach edges (no SERVER->node ip backhaul). Local heard
    # X long ago (stale), s3 is hearing it now. The coordinator must inject the backhaul AND the recency
    # cost must then pick s3. (Regression: without the inject, s3 is unreachable -> X wrongly goes local.)
    links = [
        Link(SERVER, ("endpoint", "X"), "ble-adv", rssi=-60, age_s=800),
        Link(("node", "s3"), ("endpoint", "X"), "ble-adv", rssi=-75, age_s=10),
    ]
    per_node, local_devs = compute_allowlists(links, {"X": "AA:AA"})
    assert "X" not in local_devs
    assert per_node["s3"]["device_ids"] == ["X"]


def test_build_directive_shape():
    d = build_directive(["BB:BB", "AA:AA"], 5)
    assert d["type"] == "relay_assign" and d["epoch"] == 5
    assert d["relay_macs"] == ["AA:AA", "BB:BB"] and d["cmd_relay"] == []
