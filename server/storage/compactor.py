"""
Daily compactor — flushes hot SQLite to partitioned Parquet + summary tier.

Run once per day (via systemd timer). Steps:
  1. Determine cutoff: yesterday 00:00:00 UTC (everything before stays in Parquet)
  2. Read all readings before the cutoff from SQLite
  3. Write one Parquet file per (year, month) partition (append; existing partitions
     are rewritten to include any late-arriving rows)
  4. Compute summary tier (min/max/mean/median/count/last per device+metric per day)
     and upsert into SQLite summaries table
  5. Update hash manifest (SHA-256 + size + row count per Parquet file)
  6. Delete compacted rows from SQLite hot tier

Usage:
  python3 compactor.py --db instance/db/hot.db --parquet-dir instance/db/parquet
  python3 compactor.py --dry-run   # report what would be flushed, no writes
"""

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

log = logging.getLogger("ha.compactor")

# ── Parquet schema ────────────────────────────────────────────────────────────

_SCHEMA = pa.schema([
    pa.field("ts",          pa.string()),
    pa.field("device_id",   pa.string()),
    pa.field("device_type", pa.string()),
    pa.field("area",        pa.string()),
    pa.field("transport",   pa.string()),
    pa.field("metric",      pa.string()),
    pa.field("value",       pa.float64()),
    pa.field("unit",        pa.string()),
    pa.field("schema_v",    pa.int32()),
])

_SUMMARY_DDL = """
CREATE TABLE IF NOT EXISTS summaries (
    date        TEXT    NOT NULL,
    device_id   TEXT    NOT NULL,
    metric      TEXT    NOT NULL,
    min_val     REAL,
    max_val     REAL,
    mean_val    REAL,
    median_val  REAL,
    count       INTEGER,
    last_val    REAL,
    last_ts     TEXT,
    PRIMARY KEY (date, device_id, metric)
);
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cutoff_ts() -> str:
    """Yesterday midnight UTC — everything strictly before this is compacted."""
    now = datetime.now(timezone.utc)
    yesterday = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # Subtract one day
    from datetime import timedelta
    cutoff = yesterday - timedelta(days=0)  # compact up to (not including) today
    return cutoff.strftime("%Y-%m-%dT00:00:00Z")


# ── Main compaction logic ─────────────────────────────────────────────────────

def compact(
    db_path: Path,
    parquet_dir: Path,
    dry_run: bool = False,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SUMMARY_DDL)
    conn.commit()

    cutoff = _cutoff_ts()
    log.info("Compacting rows with ts < %s (dry_run=%s)", cutoff, dry_run)

    rows = conn.execute(
        """SELECT ts, device_id, device_type, area, transport, metric, value, unit, schema_v
           FROM readings WHERE ts < ? ORDER BY ts""",
        (cutoff,),
    ).fetchall()

    if not rows:
        log.info("Nothing to compact")
        conn.close()
        return

    log.info("Found %d rows to compact", len(rows))

    # Group by (year, month) for Parquet partitioning
    from collections import defaultdict
    partitions: dict[tuple, list] = defaultdict(list)
    for row in rows:
        ts = row[0]
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            key = (dt.year, dt.month)
        except ValueError:
            key = (0, 0)
        partitions[key].append(row)

    manifest_path = parquet_dir / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        with manifest_path.open() as f:
            manifest = json.load(f).get("files", {})

    # Write Parquet partitions
    written_ids: list[int] = []
    for (year, month), partition_rows in sorted(partitions.items()):
        partition_dir = parquet_dir / f"year={year}" / f"month={month:02d}"
        if not dry_run:
            partition_dir.mkdir(parents=True, exist_ok=True)

        date_label = f"{year}-{month:02d}"
        out_path = partition_dir / f"{date_label}.parquet"

        # Build Arrow table
        col_names = ["ts", "device_id", "device_type", "area", "transport",
                     "metric", "value", "unit", "schema_v"]
        col_data = {name: [] for name in col_names}
        for row in partition_rows:
            for name, val in zip(col_names, row):
                col_data[name].append(val)

        table = pa.table(col_data, schema=_SCHEMA)

        # Merge with existing partition if present (handles late arrivals)
        if out_path.exists() and not dry_run:
            existing = pq.read_table(str(out_path), schema=_SCHEMA)
            combined = pa.concat_tables([existing, table])
            # Dedup by (ts, device_id, metric) using DuckDB window function
            _con = duckdb.connect()
            _con.register("_combined", combined)
            table = _con.execute("""
                SELECT ts, device_id, device_type, area, transport,
                       metric, value, unit, schema_v
                FROM (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY ts, device_id, metric
                               ORDER BY ts
                           ) AS _rn
                    FROM _combined
                )
                WHERE _rn = 1
                ORDER BY ts
            """).arrow()
            _con.close()

        log.info(
            "Partition year=%d month=%02d → %s (%d rows)",
            year, month, out_path, len(table),
        )

        if not dry_run:
            pq.write_table(
                table,
                str(out_path),
                compression="zstd",
                compression_level=6,
                row_group_size=100_000,
            )
            sha = _sha256_file(out_path)
            rel_path = str(out_path.relative_to(parquet_dir))
            manifest[rel_path] = {
                "sha256": sha,
                "size_bytes": out_path.stat().st_size,
                "rows": len(table),
                "updated_ts": _utc_now_iso(),
            }
            log.info("Written %s sha256=%s…", rel_path, sha[:12])

        # Track IDs to delete (use original rows, not merged table)
        written_ids.extend(
            conn.execute(
                "SELECT id FROM readings WHERE ts < ? AND ts >= ? AND ts != '0'",
                (cutoff, f"{year}-{month:02d}-01T00:00:00Z"),
            ).fetchall()
        )

    if not dry_run:
        # Update manifest
        parquet_dir.mkdir(parents=True, exist_ok=True)
        with manifest_path.open("w") as f:
            json.dump({"files": manifest, "updated_ts": _utc_now_iso()}, f, indent=2)

        # Compute and upsert summaries
        _write_summaries(conn, rows)

        # Prune compacted rows from hot tier (use timestamp range, not ID list)
        cur = conn.execute("DELETE FROM readings WHERE ts < ?", (cutoff,))
        conn.commit()
        log.info("Pruned %d rows from hot tier", cur.rowcount)

        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()

    conn.close()
    log.info("Compaction complete")


def _write_summaries(conn: sqlite3.Connection, rows: list) -> None:
    """Compute per-day per-device per-metric aggregates and upsert into summaries."""
    import statistics
    from collections import defaultdict

    # Bucket: date → device_id → metric → [values], last_ts
    buckets: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    last_ts_map: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(str)))

    for (ts, device_id, device_type, area, transport, metric, value, unit, schema_v) in rows:
        date = ts[:10]  # YYYY-MM-DD
        buckets[date][device_id][metric].append(float(value))
        if ts > last_ts_map[date][device_id][metric]:
            last_ts_map[date][device_id][metric] = ts

    summary_rows = []
    for date, devices in buckets.items():
        for device_id, metrics in devices.items():
            for metric, values in metrics.items():
                summary_rows.append((
                    date,
                    device_id,
                    metric,
                    min(values),
                    max(values),
                    sum(values) / len(values),
                    statistics.median(values),
                    len(values),
                    values[-1],
                    last_ts_map[date][device_id][metric],
                ))

    conn.executemany(
        """INSERT INTO summaries
           (date, device_id, metric, min_val, max_val, mean_val, median_val,
            count, last_val, last_ts)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(date, device_id, metric) DO UPDATE SET
             min_val=MIN(excluded.min_val, summaries.min_val),
             max_val=MAX(excluded.max_val, summaries.max_val),
             mean_val=excluded.mean_val,
             median_val=excluded.median_val,
             count=summaries.count + excluded.count,
             last_val=excluded.last_val,
             last_ts=excluded.last_ts""",
        summary_rows,
    )
    conn.commit()
    log.info("Upserted %d summary rows", len(summary_rows))


# ── Entry point ───────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Home automation Parquet compactor")
    p.add_argument("--db", default="instance/db/hot.db", type=Path)
    p.add_argument("--parquet-dir", default="instance/db/parquet", type=Path)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stdout,
    )
    compact(args.db, args.parquet_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
