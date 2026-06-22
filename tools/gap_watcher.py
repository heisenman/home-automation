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
def backfill_plan(info: dict) -> dict | None:
    """Explicit `backfill:` block wins; else infer from device_type. None = no known route (skip)."""
    bf = info.get("backfill")
    if isinstance(bf, dict) and bf.get("via"):
        return bf
    dt = (info.get("device_type") or "").lower()
    if "aranet" in dt:
        return {"via": "aranet"}
    if "outdoor" in dt:
        return {"via": "edge", "node": EDGE_NODE, "profile": "outdoor"}
    if "meter" in dt:                       # meter_pro / meter — reachable by the edge node
        return {"via": "edge", "node": EDGE_NODE, "profile": "meter_pro"}
    return None


def dispatch(mac: str, info: dict, plan: dict, dry: bool) -> None:
    via, did = plan["via"], info.get("device_id")
    if via == "edge":
        cmd = [PY, str(REPO / "tools" / "edge_pull_history.py"), "--node", plan.get("node", EDGE_NODE),
               "--mac", mac, "--profile", plan.get("profile", "outdoor"), "--broker", BROKER]
    elif via == "server":
        cmd = [PY, str(REPO / "tools" / "switchbot_history.py"), "--mac", mac,
               "--profile", plan.get("profile", "meter_pro")]
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
    p.add_argument("--lookback-days", type=int, default=int(os.environ.get("HA_GAP_LOOKBACK_DAYS", "3")))
    p.add_argument("--min-gap-min", type=float, default=float(os.environ.get("HA_GAP_MIN_MIN", "20")),
                   help="a window with no readings longer than this (minutes) is a gap")
    p.add_argument("--settle-s", type=int, default=int(os.environ.get("HA_GAP_SETTLE_S", "180")),
                   help="wait between pulls — one BLE central op at a time")
    p.add_argument("--dry-run", action="store_true", help="report what would be backfilled; pull nothing")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s")

    reg = (yaml.safe_load(a.registry.read_text()) or {}).get("devices", {})
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
        plan = backfill_plan(info)
        if not plan:
            log.info("%s: %d gap(s) (biggest %.0f min) but no backfill route — skipping", did, len(gaps), biggest)
            continue
        log.info("%s: %d gap(s), biggest %.0f min → backfill via %s", did, len(gaps), biggest, plan["via"])
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
