"""Device *relocation* — move a device (or a whole area) to a new canonical `area`, everywhere.

Sibling of ``device_migrate.py``. That tool renames a device's **identity** (``device_id``); this one changes
its **location** (``area``) — the ADR-0026 Phase-2 primitive. Kept deliberately separate (atomic tools, atomic
runbooks): identity and location are different concerns and fail differently.

``area`` is *denormalised* — stamped onto every reading at ingest — so a history-safe relocate rewrites it in
every store it was written to, **and** the registry (the authoritative source the edge-mapper reads; if that
isn't updated the mapper re-stamps the old area on the next reading and the drift returns). Idempotent,
gated, ``--dry-run``-able; the core returns a structured **report dict** (API-ready, like ``run_migration``).

Two atomic ops (+ a crosswalk driver):
  area   OLD NEW   — move every device in area OLD to NEW (a rename or a merge)
  device DEV NEW   — move one device DEV to area NEW (an override; wins over its area's mapping)

Stores handled (per host):
  - hot.db   (sqlite): ``readings.area``, ``device_last_seen.area``
  - parquet  (archive): the ``area`` column + ``manifest.json`` rebuild
  - registry (yaml)  : instance/{devices,control,*-devices}.yaml — the ``area:`` line only,
                       quote/indent/trailing-comment preserving
  - retained MQTT    : the old ``home/<old_area>/<id>/state`` path (the mapper republishes under the new area)
  - PEER (.245)      : the same hot.db area update over SSH (consistency — area isn't in the reconcile key,
                       so this is lower-stakes than a device_migrate, but kept for symmetry)

NOT touched: ``rungs.db`` / ``summaries`` (no ``area`` column — keyed by ``device_id``, which a relocate does
not change), and service restarts (the runbook's ``sudo systemctl restart ha-edge-mapper ha-api ha-api-tls``
step — the registry is the mapper's source, so it must reload). See ``docs/runbook-device-relocate.md``.
"""
from __future__ import annotations

import glob as _glob
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Reuse the generic helpers from the sibling tool rather than duplicate them.
from server.maintenance.device_migrate import (
    REPO_ROOT,
    _read_env,
    clear_retained,
    lookup_area,
    rebuild_parquet_manifest,
)

AREA_TABLES = ["readings", "device_last_seen"]     # every hot.db table carrying an `area` column

# instance/devices.yaml + control.yaml + every *-devices.yaml side-registry.
REGISTRY_FILES = ["devices.yaml", "control.yaml"]   # plus glob("*-devices.yaml"), resolved in registry_paths()

# An `area:` line in any of the registries: `area: office`, `area: "c_office"   # note`, 2- or 4-space indent.
_AREA_RE = re.compile(r'^(?P<pre>\s*area:\s*)(?P<q>["\']?)(?P<val>[^"\'#\s]+)(?P=q)(?P<post>\s*(?:#.*)?)$')


def registry_paths() -> list[Path]:
    inst = REPO_ROOT / "instance"
    paths = [inst / n for n in REGISTRY_FILES]
    paths += sorted(inst.glob("*-devices.yaml"))
    return [p for p in paths if p.exists()]


# ── sqlite store (pure; unit-tested) ─────────────────────────────────────────────────────────────────
def apply_sqlite_area(db_path: str, *, old_area: str | None = None, device_id: str | None = None,
                      new_area: str, dry_run: bool, tables: list[str] | None = None) -> dict:
    """Set ``area=new_area`` across the area-bearing tables, selecting rows either by ``old_area`` (an area
    move) or by ``device_id`` (a per-device override) — exactly one selector. Skips tables lacking the needed
    column. Idempotent: rows already at ``new_area`` are excluded, so a re-run affects 0. Returns
    ``{table: rows_affected}``. ``tables`` restricts which area-bearing tables are touched — default is all
    (``AREA_TABLES``, i.e. a history-restamp); pass ``["device_last_seen"]`` for a forward-only move that
    leaves ``readings`` history stamped with the old room."""
    if (old_area is None) == (device_id is None):
        raise ValueError("pass exactly one of old_area / device_id")
    out: dict[str, int] = {}
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in (tables or AREA_TABLES):
            if t not in have:
                continue
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
            if "area" not in cols:
                continue
            if old_area is not None:
                where, params = "area=? AND area IS NOT ?", [old_area, new_area]
            else:
                if "device_id" not in cols:
                    continue
                where, params = "device_id=? AND area IS NOT ?", [device_id, new_area]
            n = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE {where}", params).fetchone()[0]
            if not dry_run and n:
                conn.execute(f"UPDATE {t} SET area=? WHERE {where}", [new_area, *params])
            out[t] = n
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return out


def count_stale_area(db_path: str, *, old_area: str | None = None, device_id: str | None = None,
                     new_area: str | None = None, tables: list[str] | None = None) -> int:
    """Verification: rows that still carry the OLD location. For an area move that's ``area=old_area``; for an
    override it's ``device_id=dev AND area!=new_area``. Want 0 after a run. ``tables`` restricts the check to
    the tables the move actually rewrote — a forward-only move deliberately leaves ``readings`` stale, so it
    verifies only ``device_last_seen``."""
    total = 0
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in (tables or AREA_TABLES):
            if t not in have:
                continue
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
            if "area" not in cols:
                continue
            if old_area is not None:
                total += conn.execute(f"SELECT COUNT(*) FROM {t} WHERE area=?", (old_area,)).fetchone()[0]
            elif "device_id" in cols:
                total += conn.execute(f"SELECT COUNT(*) FROM {t} WHERE device_id=? AND area IS NOT ?",
                                      (device_id, new_area)).fetchone()[0]
    finally:
        conn.close()
    return total


def devices_in_area(hot_db: str, area: str) -> list[str]:
    """The device_ids currently living in ``area`` (from device_last_seen) — used to target their retained
    canonical state topics before the move."""
    try:
        conn = sqlite3.connect(hot_db, timeout=30)
        try:
            return [r[0] for r in conn.execute(
                "SELECT DISTINCT device_id FROM device_last_seen WHERE area=?", (area,))]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


# ── parquet archive (pure; unit-tested) ──────────────────────────────────────────────────────────────
def apply_parquet_area(parquet_glob: str, *, old_area: str | None = None, device_id: str | None = None,
                       new_area: str, dry_run: bool) -> dict:
    """Rewrite the ``area`` column (by ``old_area`` or by ``device_id``) in each parquet file that has stale
    rows; schema preserved. Manifest rebuild is the caller's job (once, after all files). Returns
    ``{file: rows_affected}``."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    if (old_area is None) == (device_id is None):
        raise ValueError("pass exactly one of old_area / device_id")
    out: dict[str, int] = {}
    for f in sorted(x for x in _glob.glob(parquet_glob, recursive=True) if x.endswith(".parquet")):
        t = pq.ParquetFile(f).read()
        names = t.schema.names
        if "area" not in names:
            continue
        area = t.column("area")
        if old_area is not None:
            mask = pc.equal(area, old_area)
        else:
            if "device_id" not in names:
                continue
            mask = pc.and_(pc.equal(t.column("device_id"), device_id), pc.not_equal(area, new_area))
        n = pc.sum(pc.cast(mask, pa.int64())).as_py() or 0
        if not n:
            continue
        out[Path(f).name] = n
        if dry_run:
            continue
        idx = t.schema.get_field_index("area")
        t2 = t.set_column(idx, "area", pc.if_else(mask, pa.scalar(new_area), area))
        pq.write_table(t2, f, compression="zstd")
    return out


# ── registry yaml (pure line-level; quote/indent/comment preserving; unit-tested) ────────────────────
def relocate_area_in_text(text: str, old_area: str, new_area: str) -> tuple[str, int]:
    """Rewrite every ``area: <old_area>`` line to ``new_area`` in a registry file's text, preserving the
    original quote style, indentation and trailing comment. Returns ``(new_text, lines_changed)``."""
    lines = text.splitlines(keepends=True)
    changed = 0
    for i, line in enumerate(lines):
        body, nl = _split_nl(line)
        m = _AREA_RE.match(body)
        if m and m.group("val") == old_area:
            lines[i] = f"{m.group('pre')}{m.group('q')}{new_area}{m.group('q')}{m.group('post')}{nl}"
            changed += 1
    return "".join(lines), changed


def relocate_device_in_text(text: str, device_id: str, new_area: str) -> tuple[str, int]:
    """Set the ``area:`` line of one device's entry to ``new_area`` (regardless of its old value), preserving
    formatting. Handles both registry shapes: the entry keyed *by* the device_id (control.yaml) and an entry
    carrying a ``device_id:`` field (devices.yaml / *-devices.yaml). Returns ``(new_text, 1|0)``."""
    lines = text.splitlines(keepends=True)
    rng = _entry_range(lines, device_id)
    if rng is None:
        return text, 0
    lo, hi = rng
    for i in range(lo, hi):
        body, nl = _split_nl(lines[i])
        m = _AREA_RE.match(body)
        if m and m.group("val") != new_area:
            lines[i] = f"{m.group('pre')}{m.group('q')}{new_area}{m.group('q')}{m.group('post')}{nl}"
            return "".join(lines), 1
        if m:                                   # already there — idempotent no-op
            return text, 0
    return text, 0                              # entry has no area line (nothing to move)


def _split_nl(line: str) -> tuple[str, str]:
    body = line.rstrip("\n")
    return body, line[len(body):]


def _indent(body: str) -> int:
    return len(body) - len(body.lstrip())


def _entry_range(lines: list[str], device_id: str) -> tuple[int, int] | None:
    """Return the ``[lo, hi)`` line range of the entry that owns ``device_id``, for both registry shapes."""
    key_re = re.compile(rf'^(\s*){re.escape(device_id)}\s*:\s*(#.*)?$')            # entry keyed BY device_id
    field_re = re.compile(rf'^(\s*)device_id:\s*["\']?{re.escape(device_id)}["\']?\s*(#.*)?$')  # device_id: field
    for i, line in enumerate(lines):
        body, _ = _split_nl(line)
        mk, mf = key_re.match(body), field_re.match(body)
        if mk:                                          # style A: key is the device_id; block = its children
            d = _indent(body)
            hi = i + 1
            while hi < len(lines):
                b, _ = _split_nl(lines[hi])
                if b.strip() and not b.lstrip().startswith("#") and _indent(b) <= d:
                    break
                hi += 1
            return i + 1, hi
        if mf:                                          # style B: area is a sibling of the device_id: field
            d = _indent(body)
            lo = i
            while lo > 0:                               # walk up to the entry's first sibling
                b, _ = _split_nl(lines[lo - 1])
                if b.strip() and not b.lstrip().startswith("#") and _indent(b) < d:
                    break
                lo -= 1
            hi = i + 1
            while hi < len(lines):                      # ...and down to the next entry
                b, _ = _split_nl(lines[hi])
                if b.strip() and not b.lstrip().startswith("#") and _indent(b) < d:
                    break
                hi += 1
            return lo, hi
    return None


def relocate_registry_file(path: Path, *, old_area: str | None = None, device_id: str | None = None,
                           new_area: str, dry_run: bool) -> int:
    if not path.exists():
        return 0
    text = path.read_text()
    if old_area is not None:
        new_text, n = relocate_area_in_text(text, old_area, new_area)
    else:
        new_text, n = relocate_device_in_text(text, device_id, new_area)
    if n and not dry_run:
        path.write_text(new_text)
    return n


# ── peer (thin wrapper; mirrors device_migrate.peer_hot_migrate) ─────────────────────────────────────
def peer_hot_relocate(*, old_area: str | None = None, device_id: str | None = None, new_area: str,
                      dry_run: bool, peer_host: str | None = None, key: str | None = None,
                      remote_repo: str = "/home/visko/home_automation",
                      remote_db: str = "instance/db/hot.db") -> dict:
    """Apply the same hot.db area update on the peer (.245) over SSH; backs the peer db up first. Raw sqlite3,
    no remote tool dependency. Returns ``{status, host, result|error}``."""
    peer_host = peer_host or _read_env(REPO_ROOT / "instance" / "cluster.env", "PEER_HOST")
    if not peer_host:
        return {"status": "skipped", "reason": "no PEER_HOST"}
    key = key or str(Path.home() / ".ssh" / "id_cluster")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    na = new_area.replace("'", "''")
    if old_area is not None:
        oa = old_area.replace("'", "''")
        sql = "".join(f"UPDATE {t} SET area='{na}' WHERE area='{oa}';" for t in AREA_TABLES)
        cnt = f"SELECT COUNT(*) FROM readings WHERE area='{oa}';"
    else:
        did = device_id.replace("'", "''")
        sql = "".join(f"UPDATE {t} SET area='{na}' WHERE device_id='{did}';" for t in AREA_TABLES)
        cnt = f"SELECT COUNT(*) FROM readings WHERE device_id='{did}' AND area<>'{na}';"
    if dry_run:
        remote = f"cd {remote_repo} && sqlite3 {remote_db} \"{cnt}\""
    else:
        remote = (f"cd {remote_repo} && BEFORE=$(sqlite3 {remote_db} \"{cnt}\") && "
                  f"cp {remote_db} {remote_db}.bak-{stamp} && sqlite3 {remote_db} \"{sql}\" && "
                  f"AFTER=$(sqlite3 {remote_db} \"{cnt}\") && "
                  f"echo \"readings stale before=$BEFORE after=$AFTER (backup {remote_db}.bak-{stamp})\"")
    ssh = ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
           "-o", "StrictHostKeyChecking=accept-new", f"visko@{peer_host}", remote]
    try:
        r = subprocess.run(ssh, capture_output=True, text=True, timeout=60)
    except (subprocess.SubprocessError, OSError) as e:
        return {"status": "error", "host": peer_host, "error": str(e)}
    if r.returncode != 0:
        return {"status": "error", "host": peer_host, "error": (r.stderr or r.stdout).strip()[:400]}
    return {"status": "dry-run" if dry_run else "ok", "host": peer_host, "result": r.stdout.strip()}


# ── orchestration (API-ready: returns a structured report) ───────────────────────────────────────────
def relocate(kind: str, key: str, new_area: str, *,
             hot_db: str = "instance/db/hot.db",
             parquet_glob: str = "instance/db/parquet/year=*/month=*/*.parquet",
             broker: str = "localhost", broker_port: int = 1883,
             do_peer: bool = True, do_mqtt: bool = True, do_registry: bool = True,
             backup: bool = True, dry_run: bool = False, restamp_history: bool = True) -> dict:
    """Relocate an ``area`` (``kind='area'``, ``key``=old area) or a single ``device`` (``kind='device'``,
    ``key``=device_id) to ``new_area`` across all stores + registry + peer. Returns a report dict.

    ``restamp_history`` selects the semantics: ``True`` (default) = **history-safe restamp** — rewrite the
    old room's stamp on every ``readings`` row + parquet + the .245 peer, so the past follows the device
    (correct only when the device was mislabeled from the start). ``False`` = **forward-only** — update just
    the registry (so *future* readings stamp the new room) + the ``device_last_seen`` pointer, leaving
    ``readings`` history under the old room (the right default for a genuine physical move)."""
    if kind not in ("area", "device"):
        raise ValueError(f"kind must be 'area' or 'device', got {kind!r}")
    old_area = key if kind == "area" else None
    device_id = key if kind == "device" else None
    hot_db = str((REPO_ROOT / hot_db) if not Path(hot_db).is_absolute() else hot_db)
    if not Path(parquet_glob).is_absolute():
        parquet_glob = str(REPO_ROOT / parquet_glob)

    tables = AREA_TABLES if restamp_history else ["device_last_seen"]   # forward-only leaves readings history
    report: dict = {"kind": kind, "key": key, "new_area": new_area, "dry_run": dry_run,
                    "mode": "restamp" if restamp_history else "forward", "backups": []}

    # affected devices + their old area(s) — read BEFORE mutating, for the retained-topic clear
    if kind == "area":
        affected = [(d, old_area) for d in devices_in_area(hot_db, old_area)]
    else:
        affected = [(device_id, lookup_area(hot_db, device_id))]
    report["affected_devices"] = [d for d, _ in affected]

    if backup and not dry_run:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bkdir = REPO_ROOT / "instance" / "db" / "backups"
        bkdir.mkdir(parents=True, exist_ok=True)
        if Path(hot_db).exists():
            dst = bkdir / f"{Path(hot_db).name}.{stamp}.bak"
            shutil.copy2(hot_db, dst)
            report["backups"].append(str(dst))
        if do_registry:
            for p in registry_paths():
                dst = bkdir / f"{p.name}.{stamp}.bak"
                shutil.copy2(p, dst)
                report["backups"].append(str(dst))

    sel = {"old_area": old_area} if kind == "area" else {"device_id": device_id}
    report["hot"] = apply_sqlite_area(hot_db, **sel, new_area=new_area, dry_run=dry_run, tables=tables)
    if restamp_history:
        try:
            report["parquet"] = apply_parquet_area(parquet_glob, **sel, new_area=new_area, dry_run=dry_run)
            if report["parquet"]:
                report["parquet_manifest"] = rebuild_parquet_manifest(dry_run)
        except ImportError:
            report["parquet"] = {"skipped": "pyarrow unavailable"}
    else:
        report["parquet"] = {"skipped": "forward-only (readings history retained)"}

    if do_registry:
        report["registry"] = {p.name: relocate_registry_file(p, **sel, new_area=new_area, dry_run=dry_run)
                              for p in registry_paths()}

    if do_mqtt:
        topics = [f"home/{a}/{d}/state" for d, a in affected if a]
        report["retained_cleared"] = clear_retained(broker, broker_port, topics, dry_run=dry_run)

    if do_peer and restamp_history:
        report["peer"] = peer_hot_relocate(**sel, new_area=new_area, dry_run=dry_run)
    elif do_peer:
        report["peer"] = {"skipped": "forward-only (peer readings history retained; re-seeded by sync-standby)"}

    report["verify_stale_local"] = count_stale_area(hot_db, **sel, new_area=new_area, tables=tables)
    report["clean"] = (dry_run or report["verify_stale_local"] == 0)
    return report


def apply_crosswalk(path: str = "instance/area-migration.yaml", **kw) -> dict:
    """Drive a full ADR-0026 crosswalk file: every ``renames``/``merges`` entry as an area move, then every
    ``device_overrides`` entry as a per-device override (overrides run LAST so they win). Returns
    ``{"steps": [report, ...], "clean": bool}``."""
    import yaml
    p = (REPO_ROOT / path) if not Path(path).is_absolute() else Path(path)
    doc = yaml.safe_load(p.read_text()) or {}
    steps = []
    for section in ("renames", "merges"):
        for old_area, new_area in (doc.get(section) or {}).items():
            steps.append(relocate("area", old_area, new_area, **kw))
    for device_id, new_area in (doc.get("device_overrides") or {}).items():
        steps.append(relocate("device", device_id, new_area, **kw))
    return {"steps": steps, "clean": all(s.get("clean") for s in steps)}


def _main() -> int:
    import argparse
    import json
    p = argparse.ArgumentParser(description="Relocate a device or an area to a new canonical area, everywhere")
    sub = p.add_subparsers(dest="cmd", required=True)
    pa = sub.add_parser("area", help="move every device in OLD_AREA to NEW_AREA")
    pa.add_argument("old_area"); pa.add_argument("new_area")
    pd = sub.add_parser("device", help="move one DEVICE_ID to NEW_AREA (override)")
    pd.add_argument("device_id"); pd.add_argument("new_area")
    pd.add_argument("--forward-only", action="store_true",
                    help="forward-only: leave readings history under the old room (default = history-safe restamp)")
    pc_ = sub.add_parser("crosswalk", help="apply a whole ADR-0026 crosswalk file")
    pc_.add_argument("path", nargs="?", default="instance/area-migration.yaml")
    for sp in (pa, pd, pc_):
        sp.add_argument("--dry-run", action="store_true")
        sp.add_argument("--no-peer", action="store_true", help="skip the .245 peer step")
        sp.add_argument("--no-mqtt", action="store_true", help="skip retained-topic cleanup")
        sp.add_argument("--no-registry", action="store_true", help="skip the registry yaml edits")
        sp.add_argument("--broker", default="localhost")
    a = p.parse_args()
    kw = dict(broker=a.broker, do_peer=not a.no_peer, do_mqtt=not a.no_mqtt,
              do_registry=not a.no_registry, dry_run=a.dry_run)
    if a.cmd == "crosswalk":
        rep = apply_crosswalk(a.path, **kw)
    else:
        if a.cmd == "device":
            kw["restamp_history"] = not a.forward_only
        rep = relocate(a.cmd, getattr(a, "old_area", None) or a.device_id, a.new_area, **kw)
    print(json.dumps(rep, indent=2))
    return 0 if rep.get("clean") else 2


if __name__ == "__main__":
    raise SystemExit(_main())
