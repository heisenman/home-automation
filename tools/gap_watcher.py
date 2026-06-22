#!/usr/bin/env python3
"""Daily gap watcher — find recent per-device data gaps and trigger history backfill pulls.

Runs daily (systemd ha-gap-watcher.timer). For each registered device it scans the last LOOKBACK_DAYS of
readings in hot.db for windows missing more than MIN_GAP, and only if a gap exists dispatches a history
backfill appropriate to the device TYPE. The recovery is idempotent (INSERT OR IGNORE downstream), so a
backfill only fills holes and a re-run is harmless.

Backfill methods (the "all sensors, not just SwitchBot" part — add a new `via` here per new device type):
  - edge   : signed op:history GATT pull via an edge node (tools/edge_pull_history.py) — SwitchBot meters
             the C6 can reach (attic/h_bed/c_office); recovers the meter's on-device ring buffer.
  - server : server-side GATT pull (tools/switchbot_history.py) — SwitchBot meters the .245 dongle reaches.
  - aranet : tools/aranet_history.py (get_all_records over GATT) — Aranet radon/CO2.

Routing per device comes from an explicit `backfill: {via, node, profile}` block in the registry, else is
inferred from `device_type`. Pulls run sequentially with a settle delay (one BLE central op at a time).
"""
from __future__ import annotations

import argparse
import datetime
import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
PY = str(REPO / "venv" / "bin" / "python3")
DB = Path(os.environ.get("HA_DB", str(REPO / "instance" / "db" / "hot.db")))
BROKER = os.environ.get("HA_BROKER", "localhost")
EDGE_NODE = os.environ.get("HA_EDGE_NODE", "c6-bench")
log = logging.getLogger("ha.gap_watcher")

sys.path.insert(0, str(REPO))
try:                                         # mesh-aware routing is optional (gated by --graph-routing)
    from server.mesh import store as mesh_store
    from server.mesh.topology import build_graph, best_path, hops, serialize
    _MESH = True
except Exception:                            # pragma: no cover
    _MESH = False


# ── gap detection (pure, unit-tested) ───────────────────────────────────────────
def find_gaps(times_sorted: list[float], min_gap_s: float) -> list[tuple[float, float, float]]:
    """times_sorted: ascending epoch seconds. Return [(start, end, gap_s)] for consecutive pairs whose
    spacing exceeds min_gap_s (i.e. a window with no readings)."""
    return [(a, b, b - a) for a, b in zip(times_sorted, times_sorted[1:]) if b - a > min_gap_s]


def _iso_to_epoch(s: str) -> float:
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def device_gaps(conn, device_id: str, lookback_days: int, min_gap_s: float):
    rows = conn.execute(
        "SELECT DISTINCT ts FROM readings WHERE device_id=? AND ts > datetime('now', ?) ORDER BY ts",
        (device_id, f"-{lookback_days} days")).fetchall()
    times = [_iso_to_epoch(r[0]) for r in rows]
    return find_gaps(times, min_gap_s), len(times)


# ── routing ─────────────────────────────────────────────────────────────────────
def backfill_plan(info: dict, routes: dict | None = None) -> dict | None:
    """Routing precedence: explicit `backfill:` in the registry → routes file (by device_id) → inferred
    from device_type. None = no known route (skip). The routes file keeps deployment-specific routing
    (which node reaches which meter) out of the MAC-bearing registry."""
    bf = info.get("backfill")
    if isinstance(bf, dict) and bf.get("via"):
        return bf
    r = (routes or {}).get(info.get("device_id"))
    if isinstance(r, dict) and r.get("via"):
        return r
    dt = (info.get("device_type") or "").lower()
    if "aranet" in dt:
        return {"via": "aranet"}
    if "outdoor" in dt:
        return {"via": "edge", "node": EDGE_NODE, "profile": "outdoor"}
    if "meter" in dt:                       # meter_pro / meter — reachable by the edge node
        return {"via": "edge", "node": EDGE_NODE, "profile": "meter_pro"}
    return None


def choose_plan(info: dict, routes: dict | None, conn=None) -> dict | None:
    """Mesh-aware route when the topology graph has been observed (mesh_links populated by mesh_probe),
    else the static backfill_plan. The graph picks the lowest-cost server→endpoint path, folding in each
    hop's rssi/reliability AND the terminal node's pull history (a proven puller beats a louder one that
    has only failed — the A8:02 lesson). Multi-hop paths (≥2 relays) are reported but fall back to static
    until the relay transport exists. Falls back safely whenever the graph is empty/unknown."""
    static = backfill_plan(info, routes)
    did = info.get("device_id")
    if conn is None or not _MESH or not did:
        return static
    try:
        links = mesh_store.load_links(conn)
        stats = mesh_store.pull_stats(conn)
    except Exception:
        return static                        # mesh tables not populated yet
    if not links:
        return static
    path, cost = best_path(build_graph(links), ("endpoint", did), pull_stats=stats)
    if not path:
        return static                        # graph knows nothing about this endpoint
    h = hops(path)
    if h == 0:
        plan = {"via": "server"}
    elif h == 1:
        plan = {"via": "edge", "node": path[1][1], "profile": (static or {}).get("profile", "meter_pro")}
    else:
        log.warning("%s: best path %s is %d-hop — multi-hop relay not built yet; using static route",
                    did, serialize(path), h)
        return static
    plan["graph_path"] = serialize(path)
    plan["graph_cost"] = round(cost, 2)
    return plan


def dispatch(mac: str, info: dict, plan: dict, dry: bool) -> None:
    via, did = plan["via"], info.get("device_id")
    if via == "edge":
        cmd = [PY, str(REPO / "tools" / "edge_pull_history.py"), "--node", plan.get("node", EDGE_NODE),
               "--mac", mac, "--profile", plan.get("profile", "outdoor"), "--broker", BROKER]
    elif via == "server":
        cmd = [PY, str(REPO / "tools" / "switchbot_history.py"), "--device", mac,
               "--device-id", did, "--area", info.get("area", "unknown"),
               "--device-type", info.get("device_type", "switchbot_meter_pro"),
               "--db", str(DB)]
    elif via == "aranet":
        cmd = [PY, str(REPO / "tools" / "aranet_history.py"), "--mac", mac]
    else:
        log.warning("%s: unknown backfill via=%s — skipping", did, via)
        return
    log.info("backfill %s via %s: %s", did, via, " ".join(cmd))
    if dry:
        log.info("  [dry-run] not executed")
        return
    try:
        subprocess.run(cmd, timeout=plan.get("timeout", 300), check=False)
    except subprocess.TimeoutExpired:
        log.warning("  %s backfill dispatch timed out (an edge pull still completes async)", did)


def main() -> None:
    p = argparse.ArgumentParser(description="Daily per-device gap detector + history backfill dispatcher")
    p.add_argument("--registry", default=REPO / "instance" / "devices.yaml", type=Path)
    p.add_argument("--routes", default=REPO / "instance" / "backfill-routes.yaml", type=Path,
                   help="optional device_id -> {via,node,profile} routing overrides (no MACs)")
    p.add_argument("--lookback-days", type=int, default=int(os.environ.get("HA_GAP_LOOKBACK_DAYS", "3")))
    p.add_argument("--min-gap-min", type=float, default=float(os.environ.get("HA_GAP_MIN_MIN", "20")),
                   help="a window with no readings longer than this (minutes) is a gap")
    p.add_argument("--settle-s", type=int, default=int(os.environ.get("HA_GAP_SETTLE_S", "180")),
                   help="wait between pulls — one BLE central op at a time")
    p.add_argument("--dry-run", action="store_true", help="report what would be backfilled; pull nothing")
    p.add_argument("--graph-routing", action="store_true",
                   default=os.environ.get("HA_GRAPH_ROUTING", "") not in ("", "0", "false"),
                   help="use the observed mesh topology (mesh_links) to choose the pull path; "
                        "default off → static routing (unchanged behavior)")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s")

    reg = (yaml.safe_load(a.registry.read_text()) or {}).get("devices", {})
    routes = (yaml.safe_load(a.routes.read_text()) or {}) if a.routes.exists() else {}
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    min_gap_s = a.min_gap_min * 60.0
    todo: list[tuple[str, dict, dict]] = []
    for mac, info in reg.items():
        did = info.get("device_id")
        if not did:
            continue
        gaps, n = device_gaps(conn, did, a.lookback_days, min_gap_s)
        if not gaps:
            continue
        biggest = max(g[2] for g in gaps) / 60.0
        plan = choose_plan(info, routes, conn if a.graph_routing else None)
        if not plan:
            log.info("%s: %d gap(s) (biggest %.0f min) but no backfill route — skipping", did, len(gaps), biggest)
            continue
        via_note = f" [graph {plan['graph_path']} cost={plan.get('graph_cost')}]" if plan.get("graph_path") else ""
        log.info("%s: %d gap(s), biggest %.0f min → backfill via %s%s", did, len(gaps), biggest, plan["via"], via_note)
        todo.append((mac, info, plan))

    log.info("gap watcher: %d device(s) with gaps to backfill (lookback %dd, min-gap %.0f min%s)",
             len(todo), a.lookback_days, a.min_gap_min, ", DRY-RUN" if a.dry_run else "")
    for i, (mac, info, plan) in enumerate(todo):
        dispatch(mac, info, plan, a.dry_run)
        if not a.dry_run and i + 1 < len(todo):
            time.sleep(a.settle_s)
    log.info("gap watcher done")


if __name__ == "__main__":
    main()
