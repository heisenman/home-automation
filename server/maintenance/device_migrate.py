"""Device-id migration — rename or retire a device across EVERY store, on BOTH cluster boxes.

**First-class maintenance primitive** (Hugh 2026-07-03): the backend for an eventual admin "rename / remove
device" control in the app, and the CLI for it today. Renaming or retiring a device touches many stores;
doing it by hand leaks — stale ntfy alerts, and the bidirectional `reconcile-history` merge RESURRECTS the
old id from the peer (.245). This codifies the whole procedure into one idempotent, verifiable call.

Design: the core (`run_migration`) is pure orchestration returning a structured **report dict** — no CLI or
HTTP coupling — so a future `POST /api/v1/admin/devices/{id}:rename` can call it directly and return the
report as JSON. The CLI (`python -m server.maintenance.device_migrate …`) is a thin wrapper. See
`docs/runbook-device-migrate.md`.

Stores handled (per host):
  - hot.db   (sqlite): `readings`, `device_last_seen`, `summaries`
  - rungs.db (sqlite): `rung`  (ADR-0022 ladder; also rebuilt from hot by ha-rollup.timer)
  - parquet  (archive): the `device_id` column in each file + `manifest.json`
  - retained MQTT: the old canonical `home/<area>/<id>/state` topic + `home/_alerts`
  - PEER (.245): the same hot.db migration over SSH — else `reconcile-history` re-inserts the old id.

NOT handled (separate concerns / config decisions): the registry reg-key→device_id map
(`instance/devices.yaml`), the enrollment LUT (node_id), `mesh.db` (node_id/mac) — see the runbook.
"""
from __future__ import annotations

import glob as _glob
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

HOT_TABLES = ["readings", "device_last_seen", "summaries"]   # every hot.db table with a device_id column
RUNG_TABLES = ["rung"]


# ── sqlite store (pure; unit-tested) ─────────────────────────────────────────────────────────────────
def apply_sqlite(db_path: str, tables: list[str], old_id: str, new_id: str | None,
                 *, retire: bool, dry_run: bool) -> dict:
    """Rename (device_id old→new) or retire (delete rows) across `tables` of one sqlite db. Skips tables /
    columns that don't exist. Returns {table: rows_affected}. Idempotent: a re-run affects 0 rows."""
    out: dict[str, int] = {}
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in tables:
            if t not in have:
                continue
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
            if "device_id" not in cols:
                continue
            n = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE device_id=?", (old_id,)).fetchone()[0]
            if not dry_run and n:
                if retire:
                    conn.execute(f"DELETE FROM {t} WHERE device_id=?", (old_id,))
                else:
                    conn.execute(f"UPDATE {t} SET device_id=? WHERE device_id=?", (new_id, old_id))
            out[t] = n
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return out


def count_sqlite(db_path: str, tables: list[str], device_id: str) -> int:
    """Total rows for `device_id` across `tables` — the post-migration verification (want 0 for the old id)."""
    total = 0
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in tables:
            if t not in have:
                continue
            if "device_id" not in [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]:
                continue
            total += conn.execute(f"SELECT COUNT(*) FROM {t} WHERE device_id=?", (device_id,)).fetchone()[0]
    finally:
        conn.close()
    return total


def lookup_area(hot_db: str, device_id: str) -> str | None:
    """The device's area from device_last_seen — used to target its retained canonical state topic."""
    try:
        conn = sqlite3.connect(hot_db, timeout=30)
        try:
            r = conn.execute("SELECT area FROM device_last_seen WHERE device_id=?", (device_id,)).fetchone()
            return r[0] if r else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


# ── parquet archive (pure; unit-tested) ──────────────────────────────────────────────────────────────
def apply_parquet(parquet_glob: str, old_id: str, new_id: str | None,
                  *, retire: bool, dry_run: bool) -> dict:
    """Rewrite the device_id column (rename) or drop the rows (retire) in each parquet file that contains
    `old_id`; the file schema is preserved. Returns {file: rows_affected}. Manifest rebuild is the caller's
    job (rebuild_parquet_manifest) so it runs once after all files change."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    out: dict[str, int] = {}
    for f in sorted(x for x in _glob.glob(parquet_glob, recursive=True) if x.endswith(".parquet")):
        t = pq.ParquetFile(f).read()
        if "device_id" not in t.schema.names:
            continue
        col = t.column("device_id")
        mask = pc.equal(col, old_id)
        n = pc.sum(pc.cast(mask, pa.int64())).as_py() or 0
        if not n:
            continue
        out[Path(f).name] = n
        if dry_run:
            continue
        if retire:
            t2 = t.filter(pc.invert(mask))
        else:
            idx = t.schema.get_field_index("device_id")
            t2 = t.set_column(idx, "device_id", pc.if_else(mask, pa.scalar(new_id), col))
        pq.write_table(t2, f, compression="zstd")
    return out


# ── side effects (thin wrappers; integration-verified on a real run) ─────────────────────────────────
def clear_retained(broker: str, port: int, topics: list[str], *, dry_run: bool) -> list[str]:
    """Publish an empty retained payload to each topic (deletes the retained message). Best-effort."""
    if dry_run or not topics:
        return topics
    try:
        import paho.mqtt.client as mqtt
        try:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except (AttributeError, TypeError):
            c = mqtt.Client()
        c.connect(broker, port, 10)
        c.loop_start()
        for t in topics:
            c.publish(t, payload=None, qos=1, retain=True)
        import time as _t
        _t.sleep(0.3)
        c.loop_stop()
        c.disconnect()
    except Exception as e:                                 # noqa: BLE001 — cleanup is best-effort
        return [f"ERROR: {e}"]
    return topics


def _read_env(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return None


def peer_hot_migrate(old_id: str, new_id: str | None, *, retire: bool, dry_run: bool,
                     peer_host: str | None = None, key: str | None = None,
                     remote_repo: str = "/home/visko/home_automation",
                     remote_db: str = "instance/db/hot.db") -> dict:
    """Run the SAME hot.db migration on the peer (.245) over SSH — the step that actually stops the
    reconcile-history resurrection. Raw sqlite3 (no remote python/tool dependency). Backs up the peer db
    first. Returns {status, host, affected|error}."""
    peer_host = peer_host or _read_env(REPO_ROOT / "instance" / "cluster.env", "PEER_HOST")
    if not peer_host:
        return {"status": "skipped", "reason": "no PEER_HOST"}
    key = key or str(Path.home() / ".ssh" / "id_cluster")
    rdb = f"{remote_repo}/{remote_db}"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if retire:
        sql = "".join(f"DELETE FROM {t} WHERE device_id='{old_id}';" for t in HOT_TABLES)
    else:
        sql = "".join(f"UPDATE {t} SET device_id='{new_id}' WHERE device_id='{old_id}';" for t in HOT_TABLES)
    # count-before (verify), backup, apply, count-after
    remote = (f"cd {remote_repo} && "
              f"BEFORE=$(sqlite3 {remote_db} \"SELECT COUNT(*) FROM readings WHERE device_id='{old_id}';\") && "
              f"cp {remote_db} {remote_db}.bak-{stamp} && "
              f"sqlite3 {remote_db} \"{sql}\" && "
              f"AFTER=$(sqlite3 {remote_db} \"SELECT COUNT(*) FROM readings WHERE device_id='{old_id}';\") && "
              f"echo \"readings old-id before=$BEFORE after=$AFTER (backup {remote_db}.bak-{stamp})\"")
    if dry_run:
        remote = (f"cd {remote_repo} && sqlite3 {remote_db} "
                  f"\"SELECT COUNT(*) FROM readings WHERE device_id='{old_id}';\"")
    ssh = ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
           "-o", "StrictHostKeyChecking=accept-new", f"visko@{peer_host}", remote]
    try:
        r = subprocess.run(ssh, capture_output=True, text=True, timeout=60)
    except (subprocess.SubprocessError, OSError) as e:
        return {"status": "error", "host": peer_host, "error": str(e)}
    if r.returncode != 0:
        return {"status": "error", "host": peer_host, "error": (r.stderr or r.stdout).strip()[:400]}
    return {"status": "dry-run" if dry_run else "ok", "host": peer_host, "result": r.stdout.strip()}


def rebuild_parquet_manifest(dry_run: bool) -> str:
    if dry_run:
        return "skipped (dry-run)"
    r = subprocess.run([sys.executable, str(REPO_ROOT / "tools" / "rebuild_parquet_manifest.py")],
                       capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=120)
    return (r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr) else "ok"


# ── orchestration (API-ready: returns a structured report, no I/O side-channel) ──────────────────────
def run_migration(op: str, old_id: str, new_id: str | None = None, *,
                  hot_db: str = "instance/db/hot.db", rung_db: str = "instance/db/rungs.db",
                  parquet_glob: str = "instance/db/parquet/year=*/month=*/*.parquet",
                  broker: str = "localhost", broker_port: int = 1883,
                  do_peer: bool = True, do_mqtt: bool = True, backup: bool = True,
                  dry_run: bool = False) -> dict:
    """Rename ('rename', needs new_id) or retire ('retire') a device across all stores + the peer.
    Returns a report dict (also the shape a future admin API would return). Idempotent + verifiable."""
    if op not in ("rename", "retire"):
        raise ValueError(f"op must be 'rename' or 'retire', got {op!r}")
    retire = op == "retire"
    if not retire and not new_id:
        raise ValueError("rename requires new_id")
    hot_db = str((REPO_ROOT / hot_db) if not Path(hot_db).is_absolute() else hot_db)
    rung_db = str((REPO_ROOT / rung_db) if not Path(rung_db).is_absolute() else rung_db)
    if not Path(parquet_glob).is_absolute():
        parquet_glob = str(REPO_ROOT / parquet_glob)

    report: dict = {"op": op, "old_id": old_id, "new_id": new_id, "dry_run": dry_run, "backups": []}

    # area (for the retained topic) must be read BEFORE we migrate device_last_seen
    old_area = lookup_area(hot_db, old_id)

    if backup and not dry_run:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bkdir = REPO_ROOT / "instance" / "db" / "backups"
        bkdir.mkdir(parents=True, exist_ok=True)
        for db in (hot_db, rung_db):
            if Path(db).exists():
                dst = bkdir / f"{Path(db).name}.{stamp}.bak"
                shutil.copy2(db, dst)
                report["backups"].append(str(dst))

    report["hot"] = apply_sqlite(hot_db, HOT_TABLES, old_id, new_id, retire=retire, dry_run=dry_run)
    report["rungs"] = apply_sqlite(rung_db, RUNG_TABLES, old_id, new_id, retire=retire, dry_run=dry_run) \
        if Path(rung_db).exists() else {}
    try:
        report["parquet"] = apply_parquet(parquet_glob, old_id, new_id, retire=retire, dry_run=dry_run)
        if report["parquet"]:
            report["parquet_manifest"] = rebuild_parquet_manifest(dry_run)
    except ImportError:
        report["parquet"] = {"skipped": "pyarrow unavailable"}

    if do_mqtt:
        topics = ["home/_alerts"]
        if old_area:
            topics.insert(0, f"home/{old_area}/{old_id}/state")
        report["retained_cleared"] = clear_retained(broker, broker_port, topics, dry_run=dry_run)

    if do_peer:
        report["peer"] = peer_hot_migrate(old_id, new_id, retire=retire, dry_run=dry_run)

    # verification — old id should be gone from the LOCAL stores
    report["verify_old_id_local"] = {
        "hot": count_sqlite(hot_db, HOT_TABLES, old_id),
        "rungs": count_sqlite(rung_db, RUNG_TABLES, old_id) if Path(rung_db).exists() else 0,
    }
    report["clean"] = (dry_run or sum(report["verify_old_id_local"].values()) == 0)
    return report


def _main() -> int:
    import argparse
    import json
    p = argparse.ArgumentParser(description="Rename or retire a device across every store + the peer")
    sub = p.add_subparsers(dest="op", required=True)
    pr = sub.add_parser("rename", help="rename a device_id everywhere")
    pr.add_argument("old_id"); pr.add_argument("new_id")
    pt = sub.add_parser("retire", help="remove a device_id everywhere")
    pt.add_argument("old_id")
    for sp in (pr, pt):
        sp.add_argument("--dry-run", action="store_true")
        sp.add_argument("--no-peer", action="store_true", help="skip the .245 peer step")
        sp.add_argument("--no-mqtt", action="store_true", help="skip retained-topic/alerts cleanup")
        sp.add_argument("--broker", default="localhost")
    a = p.parse_args()
    rep = run_migration(a.op, a.old_id, getattr(a, "new_id", None), broker=a.broker,
                        do_peer=not a.no_peer, do_mqtt=not a.no_mqtt, dry_run=a.dry_run)
    print(json.dumps(rep, indent=2))
    return 0 if rep.get("clean") else 2


if __name__ == "__main__":
    raise SystemExit(_main())
