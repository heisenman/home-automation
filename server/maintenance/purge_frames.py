"""purge_frames — remove a device's GARBAGE FRAMES from every history tier, then re-derive its rungs.

A "frame" is one status sample: every metric a device published at one ts. When a device emits a
recognisably bogus frame (the Midea MAD50S1QWT's mis-decoded reply: mode 0 / target 2% / RH 1% / fan 2 /
-20.7 C — rejected at the source since c6c9f27), every metric of that frame is garbage, not just the
one that identifies it. So frames are selected by a marker (`metric == value`) and removed by
(device_id, ts) across:

  - hot.db `readings`                      (DELETE)
  - the parquet archive                    (per-file rewrite, schema kept — as device_migrate.apply_parquet)
    + manifest.json                        (device_migrate.rebuild_parquet_manifest)
  - rungs.db                               (THIS device's rows re-derived from the cleaned raw; rungs hold
                                            min/max/mean, which cannot be "un-composed" surgically)

Rungs are rebuilt with the rollup module's own aggregate_raw/cascade and the same RETENTION_DAYS, so the
result is what the ladder would hold had the frames never arrived. Other devices' rungs are untouched.

SAFE BY DEFAULT: --dry-run counts only. A real run first copies hot.db / rungs.db (sqlite backup API, safe
while live) and each parquet file it will rewrite into instance/db/backups/purge-<stamp>/.

  venv/bin/python -m server.maintenance.purge_frames --device dehumidifier_living_room \\
      --marker-metric mode --marker-value 0 --dry-run
"""
from __future__ import annotations

import argparse
import glob as _glob
import shutil
import sqlite3
import time
from pathlib import Path

from server.storage import rollup

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── hot.db ────────────────────────────────────────────────────────────────────────────────────────────
_FRAME_TS = "SELECT ts FROM readings WHERE device_id=? AND metric=? AND value=?"


def purge_hot(conn: sqlite3.Connection, device: str, metric: str, value: float, *, dry_run: bool) -> int:
    """Rows (all metrics) of the marked frames. Returns rows removed (or that would be)."""
    args = (device, device, metric, value)
    n = conn.execute(f"SELECT count(*) FROM readings WHERE device_id=? AND ts IN ({_FRAME_TS})",
                     args).fetchone()[0]
    if n and not dry_run:
        with conn:
            conn.execute(f"DELETE FROM readings WHERE device_id=? AND ts IN ({_FRAME_TS})", args)
    return n


# ── parquet ───────────────────────────────────────────────────────────────────────────────────────────
def purge_parquet(parquet_glob: str, device: str, metric: str, value: float, *, dry_run: bool,
                  backup_dir: Path | None = None) -> dict:
    """{file: rows_removed} for each file holding marked frames. Manifest rebuild is the caller's job."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    out: dict[str, int] = {}
    for f in sorted(x for x in _glob.glob(parquet_glob, recursive=True) if x.endswith(".parquet")):
        t = pq.ParquetFile(f).read()                 # this file only (sibling `year` dtypes differ)
        dev = pc.equal(t.column("device_id"), device)
        marker = pc.and_(dev, pc.and_(pc.equal(t.column("metric"), metric), pc.equal(t.column("value"), value)))
        bad_ts = pc.unique(t.filter(marker).column("ts"))
        if len(bad_ts) == 0:
            continue
        drop = pc.and_(dev, pc.is_in(t.column("ts"), value_set=bad_ts))
        out[Path(f).name] = pc.sum(pc.cast(drop, pa.int64())).as_py() or 0
        if dry_run:
            continue
        if backup_dir is not None:
            shutil.copy2(f, backup_dir / Path(f).name)
        pq.write_table(t.filter(pc.invert(drop)), f, compression="zstd")
    return out


# ── rungs ─────────────────────────────────────────────────────────────────────────────────────────────
def device_raw_rows(hot: sqlite3.Connection | None, parquet_glob: str, device: str) -> list[tuple]:
    """Every raw (ts, device_id, metric, value) for the device across parquet + hot; hot wins a duplicate
    (ts, metric) — the readings API's own tie-break."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    rows: dict[tuple, float] = {}
    for f in sorted(x for x in _glob.glob(parquet_glob, recursive=True) if x.endswith(".parquet")):
        t = pq.ParquetFile(f).read(columns=["ts", "device_id", "metric", "value"])
        t = t.filter(pc.equal(t.column("device_id"), device))
        for ts, met, v in zip(t.column("ts").to_pylist(), t.column("metric").to_pylist(),
                              t.column("value").to_pylist()):
            rows[(ts, met)] = v
    if hot is not None:
        for ts, met, v in hot.execute("SELECT ts, metric, value FROM readings WHERE device_id=?", (device,)):
            rows[(ts, met)] = v
    return [(ts, device, met, v) for (ts, met), v in rows.items()]


def rebuild_device_rungs(rung_conn: sqlite3.Connection, device: str, raw_rows: list[tuple], *,
                         now_epoch: int, dry_run: bool) -> dict:
    """Replace the device's rungs with ones derived from `raw_rows`, honouring RETENTION_DAYS."""
    mem = sqlite3.connect(":memory:")
    rollup.ensure_schema(mem)
    aggs = rollup.aggregate_raw(raw_rows)
    mem.executemany(rollup._UPSERT, [("1min", d, m, b, *a) for (d, m, b), a in aggs.items()])
    hi = now_epoch // 60 * 60 + 1
    for dest, src in rollup.CASCADE:
        rollup.cascade(mem, dest, src, 0, hi)
    keep = []
    for res, *rest in mem.execute("SELECT res, device_id, metric, bucket_start, vmin, vmax, vmean, vcount, "
                                  "vlast FROM rung"):
        days = rollup.RETENTION_DAYS.get(res)
        if days is None or rest[2] >= now_epoch - days * 86400:
            keep.append((res, *rest))
    mem.close()
    before = rung_conn.execute("SELECT count(*) FROM rung WHERE device_id=?", (device,)).fetchone()[0]
    if not dry_run:
        with rung_conn:
            rung_conn.execute("DELETE FROM rung WHERE device_id=?", (device,))
            rung_conn.executemany(rollup._UPSERT, keep)
        out = {"before": before, "after": len(keep), "epoch": rollup.bump_epoch(rung_conn, now_epoch)}
        return out                                     # old buckets changed: seeded panels must re-seed
    return {"before": before, "after": len(keep)}


# ── orchestration ─────────────────────────────────────────────────────────────────────────────────────
def _backup_sqlite(src: Path, dst: Path) -> None:
    s, d = sqlite3.connect(str(src)), sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        s.close()
        d.close()


def run(device: str, metric: str, value: float, *, dry_run: bool,
        hot_db: Path = REPO_ROOT / "instance/db/hot.db",
        rung_db: Path = REPO_ROOT / "instance/db/rungs.db",
        parquet_glob: str = str(REPO_ROOT / "instance/db/parquet/year=*/month=*/*.parquet"),
        backup_root: Path = REPO_ROOT / "instance/db/backups",
        now_epoch: int | None = None, rebuild_manifest=None) -> dict:
    """`rebuild_manifest(dry_run) -> str` defaults to device_migrate's (acts on the REPO archive)."""
    now_epoch = int(time.time()) if now_epoch is None else now_epoch
    backup_dir = None
    if not dry_run:
        backup_dir = backup_root / time.strftime("purge-%Y%m%dT%H%M%SZ", time.gmtime(now_epoch))
        backup_dir.mkdir(parents=True, exist_ok=True)
        for db in (hot_db, rung_db):
            if db.exists():
                _backup_sqlite(db, backup_dir / db.name)
    report: dict = {"device": device, "marker": f"{metric}={value}", "dry_run": dry_run,
                    "backup": str(backup_dir) if backup_dir else None}
    hot = sqlite3.connect(str(hot_db)) if hot_db.exists() else None
    try:
        report["hot"] = purge_hot(hot, device, metric, value, dry_run=dry_run) if hot else 0
        report["parquet"] = purge_parquet(parquet_glob, device, metric, value, dry_run=dry_run,
                                          backup_dir=backup_dir)
        if report["parquet"]:
            if rebuild_manifest is None:
                from server.maintenance.device_migrate import rebuild_parquet_manifest as rebuild_manifest
            report["parquet_manifest"] = rebuild_manifest(dry_run)
        if rung_db.exists():
            raw = device_raw_rows(hot, parquet_glob, device)
            if dry_run:                                   # nothing was deleted: preview what the rebuild sees
                marked = _marked(raw, metric, value)       # once — per-row was O(n^2) (hours on ha-2)
                raw = [r for r in raw if (r[0], r[2]) not in marked]
            rc = sqlite3.connect(str(rung_db))
            try:
                report["rungs"] = rebuild_device_rungs(rc, device, raw, now_epoch=now_epoch, dry_run=dry_run)
            finally:
                rc.close()
    finally:
        if hot is not None:
            hot.close()
    return report


def _marked(raw: list[tuple], metric: str, value: float) -> set:
    bad = {ts for ts, _d, m, v in raw if m == metric and v == value}
    return {(ts, m) for ts, _d, m, _v in raw if ts in bad}


def _main() -> int:
    import json
    p = argparse.ArgumentParser(description="Purge a device's garbage frames from hot.db + parquet; rebuild "
                                            "its rungs")
    p.add_argument("--device", required=True)
    p.add_argument("--marker-metric", required=True, help="metric identifying a garbage frame (e.g. mode)")
    p.add_argument("--marker-value", required=True, type=float, help="its garbage value (e.g. 0)")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.device, a.marker_metric, a.marker_value, dry_run=a.dry_run), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
