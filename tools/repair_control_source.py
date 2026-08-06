#!/usr/bin/env python3
"""
Repair automation policies whose `source_sensor` names a device that no longer exists.

Symptom: an actuator's card sits permanently at health=stale, "from <some-id>", and nothing it is told
to do ever happens. The policy is fine, the device is fine — the source is a GHOST. This is what a
rename leaves behind: `device_relocate` / `apply_rename_worksheet` rewrite the registry and migrate the
history, but automation_policy.source_sensor is a free-text device_id they never touched, so the loop
keeps asking for readings from an id that stopped existing. Nothing errors. The reading is simply never
found, and "source not found" is indistinguishable from "sensor offline" — so it fails safe to the
device's default, forever, quietly.

Found in the wild 2026-08-06: purifier_living_room still pointed at `levoit_office`, retired in the
rename to purifier_living_room and with ZERO rows ever recorded, with automation disabled on top. The
purifier could not participate in anything.

A source is DANGLING when it has never produced a single row of the policy's control metric in the
readings store. For a SELF-SOURCING device (one that reports the control metric itself — the purifier's
own PM2.5), the repair is unambiguous: point it at itself. Anything else needs a human to choose, so
this tool reports it and changes nothing.

SAFE BY DEFAULT: --dry-run previews with zero writes; --enable is opt-in and never implied.

  # preview both boxes' worth of damage (no writes)
  python3 tools/repair_control_source.py --dry-run

  # repair self-sourcing devices, leave enable/disable alone
  python3 tools/repair_control_source.py

  # repair AND re-enable automation for anything it repaired
  python3 tools/repair_control_source.py --enable
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from server.control.controller import control_metric  # noqa: E402

DEFAULT_CONTROL_DB = REPO / "instance/db/control.db"
DEFAULT_HOT_DB = REPO / "instance/db/hot.db"


def _ever_reported(hot, device_id: str, metric: str) -> bool:
    """Has this device EVER produced this metric? Deliberately unbounded in time — we are distinguishing
    'ghost id' from 'currently offline', and an offline-but-real sensor still has history."""
    row = hot.execute("SELECT 1 FROM readings WHERE device_id=? AND metric=? LIMIT 1",
                      (device_id, metric)).fetchone()
    return row is not None


def audit(control_db: Path, hot_db: Path):
    """-> [{device_id, metric, source, dangling, self_sourcing, enabled, policy}] for every policy."""
    ctl = sqlite3.connect(f"file:{control_db}?mode=ro", uri=True)
    hot = sqlite3.connect(f"file:{hot_db}?mode=ro", uri=True)
    out = []
    try:
        for device_id, raw, _ in ctl.execute("SELECT device_id, json, updated_ts FROM automation_policy"):
            pol = json.loads(raw)
            metric = control_metric(pol)
            source = pol.get("source_sensor")
            out.append({
                "device_id": device_id,
                "metric": metric,
                "source": source,
                "enabled": bool(pol.get("enabled", True)),
                # a source that has never reported the control metric is a ghost, not a quiet sensor
                "dangling": bool(source) and not _ever_reported(hot, source, metric),
                # can the actuator supply its own control metric? then the repair is unambiguous
                "self_sourcing": _ever_reported(hot, device_id, metric),
                "policy": pol,
            })
    finally:
        ctl.close()
        hot.close()
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--control-db", type=Path, default=DEFAULT_CONTROL_DB)
    ap.add_argument("--hot-db", type=Path, default=DEFAULT_HOT_DB)
    ap.add_argument("--dry-run", action="store_true", help="report only; make no writes")
    ap.add_argument("--enable", action="store_true",
                    help="also set enabled=true on any policy this run repairs")
    a = ap.parse_args()

    rows = audit(a.control_db, a.hot_db)
    if not rows:
        print("no automation policies found")
        return 0

    print(f"{'device':28s} {'metric':14s} {'source':26s} enabled  state")
    for r in rows:
        state = "DANGLING" if r["dangling"] else "ok"
        print(f"{r['device_id']:28s} {r['metric']:14s} {str(r['source']):26s} "
              f"{str(r['enabled']):7s}  {state}")

    broken = [r for r in rows if r["dangling"]]
    if not broken:
        print("\nnothing to repair")
        return 0

    repaired, needs_human = [], []
    for r in broken:
        if r["self_sourcing"]:
            repaired.append(r)
        else:
            needs_human.append(r)

    for r in needs_human:
        print(f"\n! {r['device_id']}: source {r['source']!r} is a ghost and the device does not report "
              f"{r['metric']} itself — pick a source in the PWA (Automation → Sensor to follow). "
              f"Not guessing one.")

    if not repaired:
        return 1 if needs_human else 0

    conn = None if a.dry_run else sqlite3.connect(a.control_db)
    try:
        for r in repaired:
            pol = dict(r["policy"])
            was = pol.get("source_sensor")
            pol["source_sensor"] = r["device_id"]           # self-sourced: point it at itself
            note = f"\n→ {r['device_id']}: source {was!r} → {r['device_id']!r} (self-sourced)"
            if a.enable and not pol.get("enabled", True):
                pol["enabled"] = True
                note += " + automation ENABLED"
            print(note + ("   [dry-run, not written]" if a.dry_run else ""))
            if conn is not None:
                conn.execute("UPDATE automation_policy SET json=? WHERE device_id=?",
                             (json.dumps(pol), r["device_id"]))
        if conn is not None:
            conn.commit()
    finally:
        if conn is not None:
            conn.close()

    if not a.dry_run:
        print("\nwritten. ha-controller re-reads policy every tick — no restart needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
