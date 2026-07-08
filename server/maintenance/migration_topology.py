#!/usr/bin/env python3
"""migration_topology.py — air-gap Phase-4 migration planner (read-only analysis).

Turns the live mesh reach census into a migration plan so device moves keep data flowing. It answers,
per sensor: which edge node is the PRIMARY relay, and which nodes are the FALLBACK path; then derives a
chunked, one-node-at-a-time migration ORDER that never leaves a sensor without air-gap coverage.

Load-bearing fact it relies on: a repoint is NETWORK-ONLY — the node stays physically put, so its BLE
reach is unchanged. Moving a node therefore only ADDS air-gap coverage (it re-points everything it hears
at ha-2); it never removes coverage. A BLE sensor is air-gap-safe the instant ANY node hearing it moves,
and the pending-hold retire (device_push) ensures .210 never drops a sensor before ha-2 confirms it.

Sources (all read-only, no broker needed):
  * instance/devices.yaml — device_id, device_type, area, and (for esp32-backed) node_id
  * instance/db/mesh.db    — relay_state (current primary assignment) + mesh_links (reach census w/ adv_score)
  * instance/db/hot.db     — device_last_seen (OPTIONAL): flags nodes already off the household broker

Usage:
  python3 server/maintenance/migration_topology.py            # coverage map + warnings
  python3 server/maintenance/migration_topology.py --order    # + the chunked migration order
  python3 server/maintenance/migration_topology.py --json     # machine-readable

Reusable — rerun any time; it re-reads the current mesh, so it reflects rebalances and already-moved nodes.
"""
import argparse, json, sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEVICES = REPO / "instance/devices.yaml"
MESH_DB = REPO / "instance/db/mesh.db"
HOT_DB = REPO / "instance/db/hot.db"

# adv_score (mesh_links) is the coordinator's coverage-quality metric. Empirically real relays score
# ~6-10; nodes that merely graze a sensor score ~1.0-1.1. STRONG separates a usable relay from noise.
STRONG_ADV = 5.0
STALE_MIN = 15.0                              # a node quiet on .210 this many min (vs freshest) reads as migrated
BLE_TYPES = ("switchbot", "aranet")          # device_type substrings that ride the BLE mesh
GAS_TYPES = ("gas",)                          # node-local I2C sensors (no fallback — move with the node)
PANEL_HINTS = ("d1001",)                      # reTerminal panel — ESP-IDF, S3-class (repoint after S3 wiring)
ESPHOME_HINTS = ("e1001", "levoit")           # ESPHome — compiled broker/SSID; rebuild+OTA, migrate last


def load_devices(path=DEVICES):
    import yaml
    dv = (yaml.safe_load(Path(path).read_text()) or {}).get("devices", {})
    out = {}
    for key, rec in dv.items():
        if not isinstance(rec, dict) or not rec.get("device_id"):
            continue
        did = rec["device_id"]
        cur = out.setdefault(did, {"type": rec.get("device_type"), "area": rec.get("area"),
                                   "node_id": rec.get("node_id"),
                                   "mac": key.upper() if ":" in str(key) else None})
        if rec.get("node_id") and not cur["node_id"]:
            cur["node_id"] = rec["node_id"]
    return out


def load_mesh(path=MESH_DB):
    """(relay, reach, as_of). relay: {node: set(relayed macs/gas-keys)}. reach: {device_id:
    [(node, rssi, adv)]} sorted best-first. as_of: newest census ts."""
    relay, reach, as_of = {}, {}, None
    if not Path(path).exists():
        return relay, reach, as_of
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for node, macs in con.execute("SELECT node_id, relay_macs FROM relay_state"):
            try:
                relay[node] = {m.upper() for m in json.loads(macs or "[]")}
            except Exception:
                relay[node] = set()
        # Include the dictator's OWN scanner (src_kind='server', src_id='server') as a coverage source:
        # a sensor it hears is ingested directly (no edge relay). It is NOT a migratable node, and its
        # air-gap coverage depends on ha-2's physical location — analyze() flags server-only sensors.
        for src, dst, rssi, adv, ts in con.execute(
                "SELECT src_id, dst_id, rssi, adv_score, last_ts FROM mesh_links "
                "WHERE link_kind='ble-adv' AND src_kind IN ('node','server')"):
            reach.setdefault(dst, []).append((src, rssi, adv or 0.0))
            as_of = ts if (as_of is None or (ts and ts > as_of)) else as_of
    finally:
        con.close()
    for d in reach:
        reach[d].sort(key=lambda x: -x[2])
    return relay, reach, as_of


def migrated_nodes(devices, path=HOT_DB, stale_min=STALE_MIN):
    """Nodes NO LONGER feeding the household broker (.210) — their node-local gas device is stale in
    hot.db device_last_seen relative to the freshest row. A repointed node stops reporting to .210, so
    this reflects real migration progress however the move was done. Best-effort; empty if hot.db absent."""
    from datetime import datetime
    p = Path(path)
    if not p.exists():
        return set()

    def _dt(s):
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except Exception:
            return None
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        rows = {d: _dt(t) for d, t in con.execute("SELECT device_id, last_ts FROM device_last_seen")}
    except Exception:
        return set()
    finally:
        con.close()
    fresh = [t for t in rows.values() if t]
    if not fresh:
        return set()
    newest = max(fresh)
    out = set()
    for did, meta in devices.items():
        if "gas" not in str(meta["type"] or "") or not meta.get("node_id"):
            continue
        seen = rows.get(did)
        if seen is None or (newest - seen).total_seconds() / 60.0 > stale_min:
            out.add(meta["node_id"])
    return out


def node_board(node_id, edge_dir=None):
    """'esp32c6' | 'esp32s3-eth' from edge/*/nodes.yaml, or a class for non-manifest nodes:
    'panel' (d1001), 'esphome' (e1001/levoit), else 'other'."""
    import yaml
    base = Path(edge_dir) if edge_dir else REPO / "edge"
    for man in sorted(base.glob("*/nodes.yaml")):
        try:
            doc = yaml.safe_load(man.read_text()) or {}
        except Exception:
            continue
        nodes = doc.get("nodes", doc) if isinstance(doc, dict) else {}
        if isinstance(nodes, dict) and node_id in nodes:
            return man.parent.name
    low = node_id.lower()
    if any(h in low for h in ESPHOME_HINTS):
        return "esphome"
    if any(h in low for h in PANEL_HINTS):
        return "panel"
    return "other"


def analyze(devices, relay, reach, migrated=frozenset(), edge_dir=None):
    """Per-sensor coverage + per-node footprint + warnings. Pure over the loaded data (no I/O besides
    node_board's manifest read). Coverage is scored over REGISTERED sensors only."""
    mac2id = {m["mac"]: d for d, m in devices.items() if m["mac"]}
    relays_ids = {n: {mac2id.get(m, m) for m in macs} for n, macs in relay.items()}

    def primary_of(did):
        return next((n for n, ids in relays_ids.items() if did in ids), None)

    ble, gas, warnings = [], [], []
    strong_by_node = {}
    for did, meta in sorted(devices.items(), key=lambda x: (x[1]["area"] or "", x[0])):
        t = str(meta["type"] or "")
        if any(b in t for b in BLE_TYPES):
            heard = reach.get(did, [])
            strong = [(n, round(adv, 1)) for n, _, adv in heard if adv >= STRONG_ADV]
            for n, adv in strong:
                strong_by_node.setdefault(n, []).append((did, adv))
            ble.append({"device_id": did, "area": meta["area"], "primary": primary_of(did),
                        "strong": strong})
            node_strong = [s for s in strong if s[0] != "server"]
            if not heard:
                warnings.append(f"{did}: UNHEARD by any node or the server scanner — no path (coverage hole)")
            elif not node_strong:
                srv = next((f"{n}({a})" for n, a in strong if n == "server"), "weakly")
                warnings.append(f"{did}: heard ONLY by the server/dictator scanner ({srv}) — no edge relay; "
                                f"on air-gap needs ha-2's own scanner in range (or an edge node carrying the decoder)")
            elif len(node_strong) <= 1:
                warnings.append(f"{did}: single strong edge relay ({node_strong[0][0]}) — no real fallback; its move blips this sensor")
        elif any(g in t for g in GAS_TYPES):
            gas.append({"device_id": did, "area": meta["area"], "node_id": meta["node_id"]})

    # per-node footprint over REGISTERED sensors only (raw endpoints / other-node beacons don't count)
    nodes = {}
    candidates = set(relays_ids) | set(strong_by_node) | {g["node_id"] for g in gas if g["node_id"]}
    for n in candidates:
        if not n or n == "server":       # the dictator's own scanner is not a migratable node
            continue
        board = node_board(n, edge_dir)
        carries_ble = [b["device_id"] for b in ble if b["primary"] == n]
        strong_for = [d for d, _ in strong_by_node.get(n, [])]
        carries_gas = [g["device_id"] for g in gas if g["node_id"] == n]
        cov = round(sum(adv for _, adv in strong_by_node.get(n, [])), 1)
        # drop pure noise: an 'other'-class id that neither relays, hosts gas, nor is a strong relay
        if board == "other" and not (carries_ble or strong_for or carries_gas or n in relays_ids):
            continue
        nodes[n] = {"ble_primary": carries_ble, "ble_strong_for": strong_for, "gas": carries_gas,
                    "coverage": cov, "board": board, "migrated": n in migrated}
    return {"ble": ble, "gas": gas, "nodes": nodes, "warnings": warnings}


def suggest_order(rep):
    """Chunked, one-at-a-time order. Only nodes still on household are scheduled. Movable C6s first
    (gas-only, then hubs by descending coverage so air-gap coverage is front-loaded); then BLE-sensor
    retires; then S3/panel (gated on S3 app_main wiring); ESPHome last."""
    nodes = rep["nodes"]
    live = {n: d for n, d in nodes.items() if not d["migrated"]}
    done = sorted(n for n, d in nodes.items() if d["migrated"])
    c6 = {n: d for n, d in live.items() if d["board"] == "esp32c6"}
    gas_only = sorted(n for n, d in c6.items() if not d["ble_primary"] and not d["ble_strong_for"])
    hubs = sorted((n for n in c6 if n not in gas_only), key=lambda n: -c6[n]["coverage"])
    s3 = sorted(n for n, d in live.items() if d["board"] in ("esp32s3-eth", "panel"))
    esphome = sorted(n for n, d in live.items() if d["board"] == "esphome")

    chunks = []
    if done:
        chunks.append(("0. Already migrated (off the household broker)", [(n, "done") for n in done]))
    if gas_only:
        chunks.append(("1. Gas-only C6s (BLE-negligible — safest live nodes)",
                       [(n, f"relays only its gas {nodes[n]['gas']}; ~no BLE impact; ~1min gas blip") for n in gas_only]))
    if hubs:
        chunks.append(("2. BLE hubs (front-load air-gap coverage; move highest-coverage first)",
                       [(n, f"primary={nodes[n]['ble_primary']} strong-for={nodes[n]['ble_strong_for']} gas={nodes[n]['gas']} cov={nodes[n]['coverage']}") for n in hubs]))
    chunks.append(("3. Retire BLE meters on .210 (no repoint — ha-2 already hears them; device_push confirm+pending-hold)",
                   [(b["device_id"], f"area={b['area']}") for b in rep["ble"]]))
    if s3:
        chunks.append(("4. S3 / panel class (GATED on S3 app_main wiring — rollout part 4; move panel LAST as universal fallback)",
                       [(n, f"board={nodes[n]['board']} primary={nodes[n]['ble_primary']} gas={nodes[n]['gas']} cov={nodes[n]['coverage']}") for n in s3]))
    if esphome:
        chunks.append(("5. ESPHome (compiled broker/SSID — rebuild+OTA, LAST)",
                       [(n, "no runtime repoint op") for n in esphome]))
    return chunks


def _print(rep, show_order):
    print("=== AIR-GAP MIGRATION TOPOLOGY (Phase 4) ===\n")
    print("-- BLE sensors: primary relay + strong fallbacks (adv_score; strong >= %.1f) --" % STRONG_ADV)
    for b in rep["ble"]:
        strong = ", ".join(f"{n}({a})" for n, a in b["strong"]) or "— NONE"
        print(f"  {b['device_id']:24s} area={str(b['area']):12s} primary={str(b['primary']):13s} strong: {strong}")
    print("\n-- Node-local gas (no fallback — moves with its node) --")
    for g in rep["gas"]:
        print(f"  {g['device_id']:16s} area={str(g['area']):10s} node={g['node_id']}")
    print("\n-- Nodes: footprint --")
    for n, d in sorted(rep["nodes"].items(), key=lambda x: -x[1]["coverage"]):
        tag = "  [MIGRATED]" if d["migrated"] else ""
        print(f"  {n:16s} board={d['board']:12s} cov={d['coverage']:6}  primary={d['ble_primary']}  gas={d['gas']}{tag}")
    if rep["warnings"]:
        print("\n-- WARNINGS --")
        for w in rep["warnings"]:
            print(f"  - {w}")
    if show_order:
        print("\n=== SUGGESTED MIGRATION ORDER (one node at a time; confirm on ha-2 between moves) ===")
        for title, items in suggest_order(rep):
            print(f"\n{title}")
            for tgt, why in items:
                print(f"    - {tgt:22s} {why}")


def main():
    ap = argparse.ArgumentParser(description="Air-gap Phase-4 migration planner (read-only)")
    ap.add_argument("--devices", default=str(DEVICES))
    ap.add_argument("--mesh-db", default=str(MESH_DB))
    ap.add_argument("--hot-db", default=str(HOT_DB))
    ap.add_argument("--order", action="store_true", help="also print the chunked migration order")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args()
    devices = load_devices(a.devices)
    relay, reach, as_of = load_mesh(a.mesh_db)
    migrated = migrated_nodes(devices, a.hot_db)
    rep = analyze(devices, relay, reach, migrated=migrated)
    if a.json:
        print(json.dumps({"as_of": as_of, **rep, "order": suggest_order(rep) if a.order else None}, indent=2))
        return
    if as_of:
        print(f"(reach census as of {as_of}; migrated nodes detected: {sorted(migrated) or 'none'})\n")
    _print(rep, a.order)


if __name__ == "__main__":
    main()
