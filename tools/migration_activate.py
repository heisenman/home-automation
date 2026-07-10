#!/usr/bin/env python3
"""migration_activate.py — assert a migrated device is ACTIVATED on its DESTINATION before the source lets go.

The silent-drop class (2026-07-10, ADR-0032 / [[migrated-device-silent-drop]]): a device migrates to a new
home, publishes live to the destination broker, but its destination registry still carries the SOURCE's
"deregistered here" comment — so the ingest bridge sees it as UNKNOWN and drops (now quarantines) every reading.
`airgap_router_pm`+`failover_pm` bled ~23 h that way. `device_push.push()` has a logging gate (`confirm_on_ha2`),
but ONLY for that path — the incident came in via a raw config-sync + repoint that never called it.

This is the always-runnable, path-independent assertion the config-sync path lacked. Run it ON the destination
(authoritative: real registries + hot.db + quarantine) after ANY migration and BEFORE the source deregisters.

A device is ACTIVATED when BOTH hold on the destination:
  1. REGISTERED — its device_id is in one of the ingest registries (tasmota/levoit/ble sensor registries or the
     control.yaml actuator registry), uncommented. A commented entry simply doesn't parse ⇒ not registered.
  2. LOGGING    — it has fresh data (`device_last_seen.last_ts`, else MAX(readings.ts)) within --max-age.
If it's NOT logging, we also report any live-but-unregistered QUARANTINE captures — the smoking gun of the bug.

  # single-device activate gate (run on the destination)
  tools/migration_activate.py check airgap_router_pm
  tools/migration_activate.py check e1001_c_office --max-age 300 --alert   # tighter + alert on failure

  # system-wide sweep (FOLLOWUPS #3): every live-but-unregistered device on this box
  tools/migration_activate.py sweep
  tools/migration_activate.py sweep --alert

Exit: 0 = ACTIVATED / sweep clean · 2 = NOT_ACTIVATED / sweep found victims · 3 = usage/IO error.
JSON report to stdout (report-dict convention — same core can back an admin endpoint later).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import yaml  # noqa: E402
from server.ingest.tasmota_bridge import load_registry as _tas_reg   # noqa: E402  (canonical parsers, no drift)
from server.ingest.levoit_bridge import load_registry as _lev_reg    # noqa: E402
from server.maintenance.pending_sweeper import _latest_age, _iso_epoch  # noqa: E402  (reuse freshness logic)


def _ble_reg(path: Path) -> dict[str, dict]:
    """BLE/SwitchBot registry: {MAC -> entry}. Mirrors server/ingest/scanner.load_registry (that module
    isn't cleanly importable from repo-root — it does a run-from-dir `from decoders import ...`)."""
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text()) or {}
    return {str(mac).upper(): (info or {}) for mac, info in (raw.get("devices") or {}).items()}

INSTANCE = REPO / "instance"
HOT_DB = INSTANCE / "db" / "hot.db"
QUAR_DB = INSTANCE / "db" / "quarantine.db"
FRESH_S = 900   # default logging-freshness window (15 min). device_push's post-repoint gate uses a tighter 180s.


def registered_map(instance: Path) -> dict[str, str]:
    """device_id -> the registry file that registers it, across EVERY ingest registry (the union a device
    could legitimately live in). Uses each bridge's own load_registry so this set == what actually gets
    bridged (a commented/deregistered entry never parses, so it's correctly absent)."""
    out: dict[str, str] = {}

    def add(ids, label):
        for did in ids:
            if did:
                out.setdefault(str(did), label)

    add((v.get("device_id") for v in _tas_reg(instance / "tasmota-devices.yaml").values()), "tasmota-devices.yaml")
    add((v.get("device_id") for v in _lev_reg(instance / "levoit-devices.yaml").values()), "levoit-devices.yaml")
    # BLE registry values are the raw entry dicts (device_id is the canonical field the scanner stamps)
    add((v.get("device_id") or v.get("id") for v in _ble_reg(instance / "devices.yaml").values()), "devices.yaml")
    ctrl_path = instance / "control.yaml"
    if ctrl_path.exists():
        ctrl = yaml.safe_load(ctrl_path.read_text()) or {}
        add((ctrl.get("devices") or {}).keys(), "control.yaml")  # actuator registry is keyed BY device_id
    return out


def _norm(s: str) -> str:
    return str(s).lower().replace("-", "").replace("_", "")


def _quar_matches(qstore, device_id: str) -> list[dict]:
    """Live-but-unregistered quarantine captures whose identity plausibly IS this device (loose match on the
    normalised id — a migrated device's raw topic/MAC/node usually embeds its id)."""
    if qstore is None:
        return []
    nid = _norm(device_id)
    hits = []
    for d in qstore.list_devices():
        ident = str(d.get("identity", ""))
        likely = nid in _norm(ident) or _norm(ident) in nid
        hits.append({"source": d.get("source"), "identity": ident, "samples": d.get("sample_count"),
                     "last_seen": d.get("last_seen"), "likely_this_device": likely})
    # surface likely matches first
    return sorted(hits, key=lambda h: not h["likely_this_device"])


def check_device(device_id: str, hot: sqlite3.Connection, regmap: dict[str, str], qstore,
                 *, now: float, max_age: float) -> dict:
    """Pure verdict for one device on THIS (destination) system."""
    registry = regmap.get(device_id)
    registered = registry is not None
    age = _latest_age(hot, device_id, now)
    logging_ok = age is not None and age < max_age
    report = {
        "device_id": device_id,
        "registered": {"ok": registered, "registry": registry},
        "logging": {"ok": logging_ok, "age_s": (round(age) if age is not None else None),
                    "max_age_s": int(max_age), "has_any_data": age is not None},
    }
    if registered and logging_ok:
        report["verdict"] = "ACTIVATED"
        report["reason"] = f"registered in {registry} and logging (age {round(age)}s)"
    else:
        report["verdict"] = "NOT_ACTIVATED"
        if not registered:
            report["reason"] = ("NOT in any destination registry — the silent-drop signature "
                                "(migration carried the source's deregistration; bridge drops/quarantines it)")
        elif age is None:
            report["reason"] = f"registered in {registry} but has NO data on this box yet (not publishing here?)"
        else:
            report["reason"] = f"registered in {registry} but data is STALE (age {round(age)}s > {int(max_age)}s)"
        # when it isn't landing in hot.db, quarantine is where its readings actually went
        if not logging_ok:
            q = _quar_matches(qstore, device_id)
            if q:
                report["quarantine"] = {"live_but_unregistered": q,
                                        "hint": "merge with: tools/quarantine.py merge <source> <identity> "
                                                f"--device-id {device_id} --area <area> --device-type <type> --register"}
    return report


def sweep(hot: sqlite3.Connection, regmap: dict[str, str], qstore, *, now: float) -> dict:
    """FOLLOWUPS #3: every live-but-unregistered device on this box. Primary source = the quarantine store
    (the bridges already captured exactly these). Belt-and-suspenders: any fresh device_last_seen id absent
    from every registry (shouldn't happen — the writer only records registered devices — but a since-removed
    registry entry or a manual insert would surface here)."""
    victims = []
    if qstore is not None:
        for d in qstore.list_devices():
            victims.append({"via": "quarantine", "source": d.get("source"), "identity": d.get("identity"),
                            "device_type_hint": d.get("device_type_hint"), "samples": d.get("sample_count"),
                            "first_seen": d.get("first_seen"), "last_seen": d.get("last_seen")})
    fresh_unreg = []
    for did, last_ts in hot.execute("SELECT device_id, last_ts FROM device_last_seen"):
        if did in regmap:
            continue
        e = _iso_epoch(last_ts) if last_ts else None
        age = (now - e) if e is not None else None
        if age is not None and age < FRESH_S:                      # fresh AND unregistered
            fresh_unreg.append({"via": "device_last_seen", "device_id": did, "age_s": round(age)})
    return {"quarantine_victims": victims, "fresh_but_unregistered": fresh_unreg,
            "count": len(victims) + len(fresh_unreg)}


def _alert(broker: str, port: int, payload: dict):
    try:
        import paho.mqtt.client as mqtt
        from server.util.mqtt_creds import apply_credentials
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        apply_credentials(c)
        c.connect(broker, port, keepalive=10)
        c.loop_start()
        c.publish("home/_alert/new", json.dumps(payload), qos=1).wait_for_publish(timeout=5)
        c.loop_stop(); c.disconnect()
    except Exception as e:  # noqa: BLE001
        print(f"# alert publish failed: {e}", file=sys.stderr)


def _open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _qstore(path: Path):
    if not path.exists():
        return None
    from server.ingest.quarantine import QuarantineStore
    return QuarantineStore(path)


def main(argv=None) -> int:
    # common args live on a parent parser so they work AFTER the subcommand (`sweep --alert`, `check id --alert`)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--instance", type=Path, default=INSTANCE, help="dir holding the registries (default instance/)")
    common.add_argument("--hot-db", type=Path, default=HOT_DB)
    common.add_argument("--quarantine-db", type=Path, default=QUAR_DB)
    common.add_argument("--alert", action="store_true", help="publish home/_alert/new on a failed/dirty result")
    common.add_argument("--broker", default="127.0.0.1")
    common.add_argument("--port", type=int, default=1883)
    ap = argparse.ArgumentParser(description="Assert a migrated device is registered + logging on its destination")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pc = sub.add_parser("check", parents=[common], help="assert ONE migrated device is activated on this destination")
    pc.add_argument("device_id")
    pc.add_argument("--max-age", type=float, default=FRESH_S, help=f"logging-freshness window s (default {FRESH_S})")
    sub.add_parser("sweep", parents=[common], help="list EVERY live-but-unregistered device on this box (FOLLOWUPS #3)")
    args = ap.parse_args(argv)

    if not args.hot_db.exists():
        print(json.dumps({"error": f"hot.db not found at {args.hot_db}"}), file=sys.stderr)
        return 3
    regmap = registered_map(args.instance)
    hot = _open_ro(args.hot_db)
    qstore = _qstore(args.quarantine_db)
    now = time.time()
    try:
        if args.cmd == "check":
            rep = check_device(args.device_id, hot, regmap, qstore, now=now, max_age=args.max_age)
            print(json.dumps(rep, indent=2))
            ok = rep["verdict"] == "ACTIVATED"
            if not ok and args.alert:
                _alert(args.broker, args.port, {"kind": "migration_not_activated", "device_id": args.device_id,
                                                "reason": rep.get("reason"), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            return 0 if ok else 2
        else:  # sweep
            rep = sweep(hot, regmap, qstore, now=now)
            print(json.dumps(rep, indent=2))
            if rep["count"] and args.alert:
                _alert(args.broker, args.port, {"kind": "live_but_unregistered_sweep", "count": rep["count"],
                                                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            return 0 if rep["count"] == 0 else 2
    finally:
        hot.close()


if __name__ == "__main__":
    raise SystemExit(main())
