#!/usr/bin/env python3
"""
Quarantine review CLI (ADR-0032) — the dictator's tool for unregistered-device data.

When an ingest bridge sees telemetry from a device that is live on the broker but NOT in the registry, it
no longer silently drops it — it appends the raw telemetry to a separate SQLite file
(`instance/db/quarantine.db`). This tool is how a human resolves that backlog:

    tools/quarantine.py list                      # what's being quarantined right now
    tools/quarantine.py inspect <source> <id>     # metrics/timespan/sample rows for one device
    tools/quarantine.py merge   <source> <id> --device-id X --area Y --device-type Z [--register]
                                                  # register + replay captured readings into hot.db
    tools/quarantine.py purge   <source> <id>     # user-directed deletion (junk); --merged keeps pending

`source` is the bridge tag: tasmota | levoit | edge. `id` (identity) is the Tasmota %topic%, the ESPHome
node name, or the BLE MAC — exactly as shown by `list`.

Merge is LOSSLESS: it replays every pending captured reading into hot.db under the identity you supply
(recovering the window that would otherwise have been lost), then marks those rows merged. Per the
project directive, nothing is ever auto-deleted — merged rows stay until an explicit `purge --merged`,
and a plain `purge` only removes what you name.

`--register` also appends the device to its source registry (tasmota-devices.yaml / levoit-devices.yaml /
devices.yaml) so future telemetry is ingested normally — this is the ACTIVATE-on-destination fix for the
migration silent-drop. Without it, merge prints the exact registry stanza to add by hand.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.ingest.quarantine import QuarantineStore, DEFAULT_DB  # noqa: E402

# source tag -> (registry file, is the map flat at top-level? , indent for nested)
_REGISTRIES = {
    "tasmota": ("instance/tasmota-devices.yaml", "flat"),
    "levoit":  ("instance/levoit-devices.yaml", "flat"),
    "edge":    ("instance/devices.yaml", "nested"),   # keyed by MAC under a top-level `devices:` map
}


def _fmt_age(rows_dev: dict) -> str:
    return f"{rows_dev['first_seen']} → {rows_dev['last_seen']}"


def cmd_list(store: QuarantineStore, args) -> int:
    devs = store.list_devices(include_merged=args.all)
    if not devs:
        print("quarantine is empty" + ("" if args.all else " (no pending devices; --all to include merged)"))
        return 0
    print(f"{'SOURCE':8} {'IDENTITY':26} {'SAMPLES':>7} {'STATUS':8} {'TYPE?':14} SPAN")
    for d in devs:
        print(f"{d['source']:8} {d['identity']:26.26} {d['sample_count']:>7} {d['status']:8} "
              f"{(d['device_type_hint'] or '-'):14.14} {_fmt_age(d)}")
    return 0


def cmd_inspect(store: QuarantineStore, args) -> int:
    s = store.summary(args.source, args.identity)
    if s["device"] is None and s["total_rows"] == 0:
        print(f"no quarantine records for {args.source}/{args.identity}")
        return 1
    print(json.dumps({k: v for k, v in s.items() if k not in ("first", "last")}, indent=2))
    if args.rows:
        print("\n-- sample rows --")
        for r in store.readings(args.source, args.identity, limit=args.rows):
            print(f"  {r['reading_ts'] or r['recv_ts']}  {r['status']:8} {r['metrics_json'] or r['payload'][:80]}")
    return 0


def _register(source: str, identity: str, device_id: str, area: str, device_type: str) -> str:
    """Append a registry stanza for the device. Returns the stanza text (also printed). Best-effort append
    for the flat registries; for the nested edge registry we append under the file (which is one big
    `devices:` map) and flag it for a visual check."""
    reg_file, kind = _REGISTRIES[source]
    path = Path(reg_file)
    if kind == "flat":
        stanza = (f"\n{identity}:\n"
                  f"  device_id: {device_id}\n"
                  f"  area: {area}\n"
                  f"  device_type: {device_type}\n")
    else:  # nested (edge devices.yaml) — MAC-keyed, 2-space indent under `devices:`
        stanza = (f"\n  \"{identity}\":\n"
                  f"    device_id: \"{device_id}\"\n"
                  f"    device_type: \"{device_type}\"\n"
                  f"    area: \"{area}\"\n")
    if path.exists():
        with path.open("a") as fh:
            fh.write(f"\n# quarantine-merge (ADR-0032): ACTIVATED {identity} on this registry\n")
            fh.write(stanza)
    return stanza


def cmd_merge(store: QuarantineStore, args) -> int:
    if args.source not in _REGISTRIES:
        print(f"unknown source {args.source!r} (expected: {', '.join(_REGISTRIES)})")
        return 2
    rep = store.merge(args.source, args.identity, device_id=args.device_id, area=args.area,
                      device_type=args.device_type, hot_db=args.hot_db, dry_run=args.dry_run)
    print(json.dumps(rep, indent=2))
    if not rep.get("ok"):
        return 1
    if args.register and not args.dry_run:
        _register(args.source, args.identity, args.device_id, args.area, args.device_type)
        print(f"\nregistered in {_REGISTRIES[args.source][0]} — restart the {args.source} bridge to pick it up")
    else:
        # always show the stanza so the operator can ACTIVATE the device (the real migration fix)
        reg_file, kind = _REGISTRIES[args.source]
        if kind == "flat":
            hint = (f"{args.identity}:\n  device_id: {args.device_id}\n  area: {args.area}\n"
                    f"  device_type: {args.device_type}")
        else:
            hint = (f'  "{args.identity}":\n    device_id: "{args.device_id}"\n'
                    f'    device_type: "{args.device_type}"\n    area: "{args.area}"')
        print(f"\nTO ACTIVATE going forward, add to {reg_file} (or re-run with --register):\n{hint}")
    return 0


def cmd_purge(store: QuarantineStore, args) -> int:
    if not args.yes and not args.dry_run:
        print("refusing to delete without --yes (or use --dry-run to preview)")
        return 2
    rep = store.purge(args.source, args.identity, only_merged=args.merged, dry_run=args.dry_run)
    print(json.dumps(rep, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Unregistered-device quarantine review (ADR-0032)")
    p.add_argument("--db", default=DEFAULT_DB, help="quarantine SQLite file")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="list quarantined devices")
    pl.add_argument("--all", action="store_true", help="include merged devices")
    pl.set_defaults(fn=cmd_list)

    pi = sub.add_parser("inspect", help="inspect one quarantined device")
    pi.add_argument("source")
    pi.add_argument("identity")
    pi.add_argument("--rows", type=int, default=10, help="show N sample rows (0 to hide)")
    pi.set_defaults(fn=cmd_inspect)

    pm = sub.add_parser("merge", help="register + replay captured readings into hot.db")
    pm.add_argument("source")
    pm.add_argument("identity")
    pm.add_argument("--device-id", required=True)
    pm.add_argument("--area", required=True)
    pm.add_argument("--device-type", required=True)
    pm.add_argument("--hot-db", default="instance/db/hot.db", type=Path)
    pm.add_argument("--register", action="store_true", help="also append to the source registry (ACTIVATE)")
    pm.add_argument("--dry-run", action="store_true")
    pm.set_defaults(fn=cmd_merge)

    pp = sub.add_parser("purge", help="user-directed deletion of quarantined data")
    pp.add_argument("source")
    pp.add_argument("identity")
    pp.add_argument("--merged", action="store_true", help="only delete already-merged rows (keep pending)")
    pp.add_argument("--yes", action="store_true", help="confirm the deletion")
    pp.add_argument("--dry-run", action="store_true")
    pp.set_defaults(fn=cmd_purge)

    args = p.parse_args()
    store = QuarantineStore(args.db)
    try:
        return args.fn(store, args)
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
