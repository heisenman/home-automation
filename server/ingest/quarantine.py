#!/usr/bin/env python3
"""
Unregistered-device data quarantine (ADR-0032).

The ingest bridges (tasmota_bridge, levoit_bridge, edge_mapper) translate a device's raw telemetry into
the canonical `home/<area>/<device_id>/state` message the writer persists. That translation needs a
REGISTRY entry (device_id + area + device_type). If a device is **live on the broker but unregistered**
— e.g. a migrated device whose destination registry still carries the source's "deregistered here"
comment (the 2026-07-10 `airgap_router_pm`/`failover_pm` incident that lost ~23 h of readings) — the
bridge historically just logged "UNKNOWN …" once and **dropped** every message. Silent data loss.

This module is the safety net: instead of dropping, a bridge hands the raw telemetry to a
`QuarantineSink`, which appends it to a **separate SQLite file** (`instance/db/quarantine.db`, NOT
hot.db). Nothing is discarded and nothing in the happy path changes — quarantine only ever captures what
was *already* being dropped. A human (or a future admin op) later reviews the quarantine and either
**merges** a device (register it → replay its captured readings into hot.db, so the lost window is
recovered) or **purges** it (junk). Per Hugh's directive, quarantined data is **never auto-deleted** —
only an explicit purge or a successful merge removes rows. See `tools/quarantine.py` for that workflow.

Two tables:
  quarantined_readings  — one row per captured message (raw payload verbatim + best-effort canonical
                          metrics), the durable record that makes a later merge lossless.
  quarantined_devices   — a per-(source, identity) rollup for fast listing + the "live-but-unregistered"
                          alert (first-sighting fires `home/_alert/new` if a publisher is wired in).

`QuarantineSink` is the write side (used by the bridges); `QuarantineStore` is the read/merge/purge side
(used by the CLI). Both are import-light and degrade gracefully: a bridge that can't open the quarantine
DB logs once and keeps running (it just drops, exactly as before) — quarantine must never break ingest.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("ha.quarantine")

DEFAULT_DB = os.environ.get("HA_QUARANTINE_DB", "instance/db/quarantine.db")

_DDL = """
CREATE TABLE IF NOT EXISTS quarantined_readings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,          -- bridge tag: 'tasmota' | 'levoit' | 'edge'
    identity      TEXT NOT NULL,          -- the unregistered key: Tasmota topic / ESPHome node / BLE MAC
    recv_ts       TEXT NOT NULL,          -- bridge UTC receive time (ISO-8601 Z) — always trustworthy
    reading_ts    TEXT,                   -- device-supplied ts if any (else NULL -> use recv_ts on merge)
    topic         TEXT NOT NULL,          -- raw inbound MQTT topic
    payload       TEXT NOT NULL,          -- raw inbound payload, verbatim (JSON or plain string)
    metrics_json  TEXT,                   -- best-effort canonical {metric: value} (registry-independent)
    transport     TEXT,                   -- canonical transport tag for replay (e.g. 'wifi-mqtt','ble-adv')
    status        TEXT NOT NULL DEFAULT 'pending',   -- 'pending' | 'merged'
    merged_device_id TEXT,
    merged_at     TEXT
);
-- Soft idempotency: the same message (same identity, same device ts / recv second, same topic) captured
-- twice — e.g. a retained MQTT re-delivery — collapses instead of piling up dupes.
CREATE UNIQUE INDEX IF NOT EXISTS idx_q_readings_unique
    ON quarantined_readings (source, identity, topic, COALESCE(reading_ts, recv_ts));
CREATE INDEX IF NOT EXISTS idx_q_readings_identity ON quarantined_readings (source, identity, status);
CREATE INDEX IF NOT EXISTS idx_q_readings_recv     ON quarantined_readings (recv_ts);

CREATE TABLE IF NOT EXISTS quarantined_devices (
    source           TEXT NOT NULL,
    identity         TEXT NOT NULL,
    device_type_hint TEXT,
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL,
    sample_count     INTEGER NOT NULL DEFAULT 0,
    alerted          INTEGER NOT NULL DEFAULT 0,   -- 1 once the live-but-unregistered alert has fired
    status           TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (source, identity)
);
"""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_db(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the quarantine SQLite file. WAL for concurrent bridge writers + CLI reads."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_DDL)
    conn.commit()
    return conn


class QuarantineSink:
    """Write side, held by an ingest bridge. `capture()` appends one dropped message to the quarantine.

    Never raises out to the caller: a quarantine failure must not break live ingest — it logs once and
    the bridge drops the message exactly as it did before this module existed.
    """

    def __init__(self, db_path: str | Path = DEFAULT_DB, *,
                 alert_publish: Optional[Callable[[str, str], None]] = None):
        self._alert_publish = alert_publish
        self._broken = False
        try:
            self._conn = open_db(db_path)
            log.info("quarantine sink open at %s", db_path)
        except Exception as exc:                         # pragma: no cover - disk/perm failure
            self._conn = None
            self._broken = True
            log.error("could NOT open quarantine DB at %s (%s) — unregistered data will be dropped, "
                      "not quarantined", db_path, exc)

    def capture(self, *, source: str, identity: str, topic: str, payload,
                metrics: Optional[dict] = None, reading_ts: Optional[str] = None,
                transport: Optional[str] = None, device_type_hint: Optional[str] = None,
                recv_ts: Optional[str] = None) -> None:
        if self._broken or self._conn is None:
            return
        recv_ts = recv_ts or _utc_now()
        if not isinstance(payload, str):
            try:
                payload = json.dumps(payload)
            except (TypeError, ValueError):
                payload = str(payload)
        metrics_json = None
        if metrics:
            # keep only real numeric metrics — the same shape the writer stores
            clean = {k: float(v) for k, v in metrics.items()
                     if isinstance(v, (int, float)) and not isinstance(v, bool)}
            metrics_json = json.dumps(clean) if clean else None
        try:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO quarantined_readings
                   (source, identity, recv_ts, reading_ts, topic, payload, metrics_json, transport)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (source, identity, recv_ts, reading_ts, topic, payload, metrics_json, transport),
            )
            inserted = cur.rowcount > 0
            first_time = self._bump_rollup(source, identity, recv_ts, device_type_hint, inserted)
            self._conn.commit()
        except sqlite3.Error as exc:                     # pragma: no cover
            log.error("quarantine capture failed for %s/%s: %s", source, identity, exc)
            return
        if first_time:
            log.warning("QUARANTINED new unregistered device source=%s identity=%s — review with "
                        "`tools/quarantine.py list` then merge (register+recover) or purge", source, identity)
            self._maybe_alert(source, identity)

    def _bump_rollup(self, source: str, identity: str, recv_ts: str,
                     device_type_hint: Optional[str], inserted: bool) -> bool:
        """Upsert the per-device rollup. Returns True if this is the FIRST sighting of the identity."""
        row = self._conn.execute(
            "SELECT sample_count FROM quarantined_devices WHERE source=? AND identity=?",
            (source, identity)).fetchone()
        first_time = row is None
        inc = 1 if inserted else 0
        if first_time:
            self._conn.execute(
                """INSERT INTO quarantined_devices
                   (source, identity, device_type_hint, first_seen, last_seen, sample_count, status)
                   VALUES (?,?,?,?,?,?, 'pending')""",
                (source, identity, device_type_hint, recv_ts, recv_ts, inc))
        else:
            self._conn.execute(
                """UPDATE quarantined_devices
                   SET last_seen=?, sample_count=sample_count+?,
                       device_type_hint=COALESCE(device_type_hint, ?),
                       status=CASE WHEN status='merged' THEN 'pending' ELSE status END
                   WHERE source=? AND identity=?""",
                (recv_ts, inc, device_type_hint, source, identity))
        return first_time

    def _maybe_alert(self, source: str, identity: str) -> None:
        """Publish a one-shot 'live-but-unregistered' alert so it never fails silent (best-effort)."""
        if self._alert_publish is None:
            return
        already = self._conn.execute(
            "SELECT alerted FROM quarantined_devices WHERE source=? AND identity=?",
            (source, identity)).fetchone()
        if already and already[0]:
            return
        alert = {
            "id": f"unregistered:{source}:{identity}",
            "severity": "warning",
            "kind": "live_but_unregistered",
            "device": identity,
            "source": source,
            "ts": _utc_now(),
            "message": (f"Device '{identity}' ({source}) is publishing to the broker but is NOT in the "
                        f"registry — its data is being quarantined, not stored. Register it (merge) or "
                        f"purge it: tools/quarantine.py"),
        }
        try:
            self._alert_publish("home/_alert/new", json.dumps(alert))
            self._conn.execute(
                "UPDATE quarantined_devices SET alerted=1 WHERE source=? AND identity=?",
                (source, identity))
            self._conn.commit()
        except Exception as exc:                         # pragma: no cover
            log.debug("quarantine alert publish failed for %s/%s: %s", source, identity, exc)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()


class QuarantineStore:
    """Read / merge / purge side, held by the CLI (`tools/quarantine.py`). All methods return plain dicts
    (report-dict convention) so the same core can back an admin API endpoint later."""

    def __init__(self, db_path: str | Path = DEFAULT_DB):
        self._conn = open_db(db_path)
        self._conn.row_factory = sqlite3.Row

    # ── inspection ──────────────────────────────────────────────────────────
    def list_devices(self, *, include_merged: bool = False) -> list[dict]:
        q = "SELECT * FROM quarantined_devices"
        if not include_merged:
            q += " WHERE status != 'merged'"
        q += " ORDER BY last_seen DESC"
        return [dict(r) for r in self._conn.execute(q)]

    def readings(self, source: str, identity: str, *, status: Optional[str] = None,
                 limit: Optional[int] = None) -> list[dict]:
        q = "SELECT * FROM quarantined_readings WHERE source=? AND identity=?"
        args: list = [source, identity]
        if status:
            q += " AND status=?"
            args.append(status)
        q += " ORDER BY COALESCE(reading_ts, recv_ts)"
        if limit:
            q += f" LIMIT {int(limit)}"
        return [dict(r) for r in self._conn.execute(q, args)]

    def summary(self, source: str, identity: str) -> dict:
        rows = self.readings(source, identity)
        metrics: dict[str, int] = {}
        for r in rows:
            for m in json.loads(r["metrics_json"] or "{}"):
                metrics[m] = metrics.get(m, 0) + 1
        dev = self._conn.execute(
            "SELECT * FROM quarantined_devices WHERE source=? AND identity=?",
            (source, identity)).fetchone()
        return {
            "source": source,
            "identity": identity,
            "device": dict(dev) if dev else None,
            "total_rows": len(rows),
            "pending_rows": sum(1 for r in rows if r["status"] == "pending"),
            "metrics_seen": metrics,
            "first": rows[0] if rows else None,
            "last": rows[-1] if rows else None,
        }

    # ── merge ───────────────────────────────────────────────────────────────
    def merge(self, source: str, identity: str, *, device_id: str, area: str, device_type: str,
              hot_db: str | Path, dry_run: bool = False) -> dict:
        """Replay every PENDING captured reading for this identity into hot.db under the given identity,
        then mark those rows merged. Lossless recovery of the quarantined window. Registration of the
        device in its source registry is a separate step (see tools/quarantine.py --register) so this
        core only ever touches data stores."""
        rows = self.readings(source, identity, status="pending")
        if not rows:
            return {"ok": False, "reason": "no pending rows", "source": source, "identity": identity}

        # Build canonical payloads and hand them to the writer's own insert path (identical dedup + units).
        import importlib
        writer = importlib.import_module("server.storage.writer")
        replayed = 0
        row_ids: list[int] = []
        conn = writer._open_db(Path(hot_db)) if not dry_run else None
        try:
            for r in rows:
                metrics = json.loads(r["metrics_json"] or "{}")
                if not metrics:
                    continue
                payload = {
                    "schema": 1,
                    "device_id": device_id,
                    "device_type": device_type,
                    "area": area,
                    "ts": r["reading_ts"] or r["recv_ts"],
                    "transport": r["transport"] or "unknown",
                    "metrics": metrics,
                    "meta": {"quarantine_source": source, "quarantine_identity": identity},
                }
                if not dry_run:
                    replayed += writer._insert_readings(conn, payload)
                else:
                    replayed += len(metrics)
                row_ids.append(r["id"])
        finally:
            if conn is not None:
                conn.close()

        if not dry_run and row_ids:
            self._conn.executemany(
                "UPDATE quarantined_readings SET status='merged', merged_device_id=?, merged_at=? "
                "WHERE id=?",
                [(device_id, _utc_now(), rid) for rid in row_ids])
            self._conn.execute(
                "UPDATE quarantined_devices SET status='merged' WHERE source=? AND identity=?",
                (source, identity))
            self._conn.commit()
        return {
            "ok": True, "dry_run": dry_run, "source": source, "identity": identity,
            "device_id": device_id, "area": area, "device_type": device_type,
            "rows_merged": len(row_ids), "readings_written": replayed,
        }

    # ── purge (user-directed deletion only) ──────────────────────────────────
    def purge(self, source: str, identity: str, *, only_merged: bool = False,
              dry_run: bool = False) -> dict:
        q = "DELETE FROM quarantined_readings WHERE source=? AND identity=?"
        cq = "SELECT COUNT(*) FROM quarantined_readings WHERE source=? AND identity=?"
        args = [source, identity]
        if only_merged:
            q += " AND status='merged'"
            cq += " AND status='merged'"
        n = self._conn.execute(cq, args).fetchone()[0]
        if not dry_run:
            self._conn.execute(q, args)
            if not only_merged:
                self._conn.execute(
                    "DELETE FROM quarantined_devices WHERE source=? AND identity=?", (source, identity))
            self._conn.commit()
        return {"ok": True, "dry_run": dry_run, "source": source, "identity": identity,
                "rows_deleted": n, "only_merged": only_merged}

    def close(self) -> None:
        self._conn.close()
