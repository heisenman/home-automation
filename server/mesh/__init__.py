"""Mesh topology + path-aware backfill routing.

The forwarding fabric is NOT a star — it can be a multi-hop chain:

    server ── ip ──> node_a ── espnow ──> node_b ── ble ──> endpoint

because a node's backhaul (wifi/ethernet) is not guaranteed and BLE has to cross walls. `topology`
holds the pure graph + pathfinder (no I/O, unit-tested). `store` persists observed links + pull
outcomes in hot.db. `tools/mesh_probe.py` populates the graph from what the system already sees.
"""
