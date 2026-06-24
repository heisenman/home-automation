"""ADR-0015 Phase A — best_relay() source selection + Assigner hysteresis/failover."""
from server.mesh.assign import LOCAL, Assigner
from server.mesh.topology import SERVER, Link, best_relay, build_graph

EP = ("endpoint", "m1")


def _g(local_rssi=None, edges=()):
    links = []
    if local_rssi is not None:
        links.append(Link(SERVER, EP, "ble-adv", rssi=local_rssi))
    for nid, rssi in edges:
        links.append(Link(SERVER, ("node", nid), "ip"))
        links.append(Link(("node", nid), EP, "ble-adv", rssi=rssi))
    return build_graph(links)


def test_best_relay_prefers_strong_edge_over_weak_local():
    node, hops, _ = best_relay(_g(local_rssi=-70, edges=[("c6", -50)]), EP)
    assert node == ("node", "c6") and hops == 1


def test_best_relay_prefers_local_when_signal_equal():
    # local has no IP backhaul hop, so at equal rssi the dictator's own radio wins (hops==0).
    node, hops, _ = best_relay(_g(local_rssi=-50, edges=[("c6", -50)]), EP)
    assert node == SERVER and hops == 0


def test_best_relay_unreachable():
    node, hops, cost = best_relay(build_graph([]), EP)
    assert node is None and hops == -1


def test_best_relay_reliability_fresh_edge_beats_stale_strong_local():
    # local hears it STRONGER (-55) but STALE (heard 150s ago); edge weaker (-78) but FRESH (5s).
    # Live-adv selection must prefer the steadily-heard source (reliability > raw rssi).
    g = build_graph([
        Link(SERVER, EP, "ble-adv", rssi=-55, age_s=150),
        Link(SERVER, ("node", "s3"), "ip"),
        Link(("node", "s3"), EP, "ble-adv", rssi=-78, age_s=5),
    ])
    node, hops, _ = best_relay(g, EP)
    assert node == ("node", "s3")


def test_best_relay_rate_demotes_gappy_recent_for_steady_source():
    # Both just heard (age 0) at comparable rssi, but local is GAPPY (rate ~1) and s3 is STEADY (rate ~10).
    # Recency alone can't tell them apart; the rate term must demote gappy local so steady s3 wins.
    g = build_graph([
        Link(SERVER, EP, "ble-adv", rssi=-85, age_s=0, rate=1.0),
        Link(SERVER, ("node", "s3"), "ip"),
        Link(("node", "s3"), EP, "ble-adv", rssi=-88, age_s=0, rate=10.0),
    ])
    node, _, _ = best_relay(g, EP)
    assert node == ("node", "s3")


def test_rate_is_opt_in_none_leaves_selection_unchanged():
    # rate=None (the live Assigner path) => no rate term => stronger/closer local still wins as before.
    g = build_graph([
        Link(SERVER, EP, "ble-adv", rssi=-85, age_s=0, rate=None),
        Link(SERVER, ("node", "s3"), "ip"),
        Link(("node", "s3"), EP, "ble-adv", rssi=-88, age_s=0, rate=None),
    ])
    node, hops, _ = best_relay(g, EP)
    assert node == SERVER and hops == 0


def test_assigner_cold_start_assigns_only_source():
    a = Assigner()
    a.observe("m1", LOCAL, -60, 0.0)
    assert a.preferred("m1", 0.0) == LOCAL


def test_assigner_hysteresis_holds_within_margin():
    # local -61 (cost ~5.1) vs c6 -50 (cost 5.0): challenger leads by <margin → keep incumbent.
    a = Assigner(switch_margin=0.6, switch_dwell_s=30, fresh_window_s=100)
    a.observe("m1", LOCAL, -61, 0.0)
    assert a.preferred("m1", 0.0) == LOCAL
    a.observe("m1", LOCAL, -61, 1.0)
    a.observe("m1", "c6", -50, 1.0)
    assert a.preferred("m1", 1.0) == LOCAL  # within margin → sticky


def test_assigner_switches_only_after_sustained_margin():
    a = Assigner(switch_margin=0.6, switch_dwell_s=30, fresh_window_s=100)
    a.observe("m1", LOCAL, -80, 0.0)          # weak local (cost ~7.0)
    assert a.preferred("m1", 0.0) == LOCAL
    a.observe("m1", LOCAL, -80, 5.0)
    a.observe("m1", "c6", -50, 5.0)           # strong c6 (cost 5.0) — beats by 2.0 but dwell not met
    assert a.preferred("m1", 5.0) == LOCAL
    a.observe("m1", LOCAL, -80, 40.0)
    a.observe("m1", "c6", -50, 40.0)          # sustained past dwell → switch
    assert a.preferred("m1", 40.0) == "c6"


def test_assigner_failover_when_source_goes_stale():
    a = Assigner(switch_margin=0.6, switch_dwell_s=30, fresh_window_s=100)
    a.observe("m1", "c6", -50, 0.0)
    assert a.preferred("m1", 0.0) == "c6"
    # c6 last heard at t=0; local heard fresh at t=190; at t=200 c6 is stale (>100) → failover to local
    a.observe("m1", LOCAL, -60, 190.0)
    assert a.preferred("m1", 200.0) == LOCAL
