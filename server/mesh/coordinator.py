"""ha-relay-coordinator — ADR-0015 Phase B / decision #4.

Computes each edge node's relay allowlist from the mesh reach graph (server/mesh/store mesh.db) and the
registry, and publishes a signed, retained `relay_assign` directive on `home/edge/<node>/relay` so nodes
stop relaying meters they're NOT the preferred source for (saves edge radio/energy). Tier-1 (the mapper)
already drops redundant data server-side; this stops the redundant TRANSMISSION at the node.

Contract: docs/decisions/phase-b-relay-directive-contract.md. DEFAULT DRY-RUN — prints the directives it
would send; live publish (--publish) needs the firmware consumer (dev) and per-node signing (master LUT).
The allowlist math is pure + unit-tested (compute_allowlists).
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
from server.mesh import store as mesh_store          # noqa: E402
from server.mesh.topology import SERVER, Link, best_relay, build_graph  # noqa: E402

log = logging.getLogger("ha.relay_coordinator")


def compute_allowlists(links, dev_to_mac: dict, now: float | None = None):
    """From observed links, return (per_node, local_devs):
      per_node  = {node_id: {"device_ids": [...], "relay_macs": [...]}}  — edge nodes that are the
                  preferred source for >=1 device (their relay allowlist).
      local_devs = device_ids the dictator's own radio ('local') is preferred for (no edge directive).
    A node's allowlist is exactly the devices best_relay picks IT for; everything else it should drop."""
    # mesh.db records only receiver->endpoint reach, NOT the SERVER->node IP backhaul. Inject the implicit
    # backhaul (every edge node is reachable over MQTT/IP) so best_relay can actually route through a node;
    # without it edge nodes are disconnected from SERVER and local always wins by default.
    edge_nodes = {l.src for l in links if l.src[0] == "node"}
    links = list(links) + [Link(SERVER, n, "ip") for n in edge_nodes]
    g = build_graph(links)
    endpoints = sorted({n for n in g.nodes if n[0] == "endpoint"})
    per_node: dict[str, dict] = {}
    local_devs: list[str] = []
    for ep in endpoints:
        node, hops, cost = best_relay(g, ep, src=SERVER)
        if node is None:
            continue
        did = ep[1]
        if node == SERVER:
            local_devs.append(did)
            continue
        nid = node[1]
        slot = per_node.setdefault(nid, {"device_ids": [], "relay_macs": []})
        slot["device_ids"].append(did)
        mac = dev_to_mac.get(did)
        if mac:
            slot["relay_macs"].append(mac)
    return per_node, local_devs


def build_directive(relay_macs: list[str], epoch: int, ttl_s: int = 3600) -> dict:
    return {"schema": 1, "type": "relay_assign", "epoch": epoch,
            "relay_macs": sorted(relay_macs), "cmd_relay": [], "ttl_s": ttl_s}


def _load_registry_dev_to_mac(path: Path) -> dict:
    import yaml
    raw = yaml.safe_load(path.read_text()) if path.exists() else {}
    out = {}
    for mac, info in (raw.get("devices", {}) or {}).items():
        did = info.get("device_id")
        if did:
            out[did] = mac.upper()
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="ADR-0015 Phase B relay-assignment coordinator")
    p.add_argument("--mesh-db", default="instance/db/mesh.db", type=Path)
    p.add_argument("--registry", default="instance/devices.yaml", type=Path)
    p.add_argument("--epoch", type=int, default=1, help="epoch to stamp (dry-run preview)")
    p.add_argument("--publish", action="store_true", help="actually sign+publish (needs firmware + master); default DRY-RUN")
    p.add_argument("--log-level", default="INFO")
    a = p.parse_args()
    logging.basicConfig(level=getattr(logging, a.log_level), format="%(message)s", stream=sys.stdout)

    mp = a.mesh_db if a.mesh_db.is_absolute() else REPO_ROOT / a.mesh_db
    conn = sqlite3.connect(str(mp))
    mesh_store.ensure_schema(conn)
    links = mesh_store.load_links(conn)
    dev_to_mac = _load_registry_dev_to_mac(a.registry if a.registry.is_absolute() else REPO_ROOT / a.registry)

    per_node, local_devs = compute_allowlists(links, dev_to_mac)
    print(f"# relay-coordinator (dry-run) — {len(links)} links, {len(dev_to_mac)} registered devices")
    print(f"# LOCAL (dictator radio) is preferred for {len(local_devs)} device(s): {', '.join(sorted(local_devs)) or '-'}")
    if not per_node:
        print("# No edge node is the preferred source for any device — every node's allowlist is EMPTY")
        print("#   (i.e. directives would tell edge nodes to relay nothing). See contract 'Open tuning note'.")
    for nid in sorted(per_node):
        d = build_directive(per_node[nid]["relay_macs"], a.epoch)
        print(f"\nnode {nid}: relay {len(d['relay_macs'])} device(s) -> {', '.join(per_node[nid]['device_ids'])}")
        print(f"  home/edge/{nid}/relay  {json.dumps(d)}")
    if a.publish:
        print("\n!! --publish not yet wired (needs the firmware consumer + per-node LUT signing). DRY-RUN only.")


if __name__ == "__main__":
    main()
