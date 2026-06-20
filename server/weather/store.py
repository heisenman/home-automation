"""
Weather store — writes to a `weather` table in long format (same shape as the sensor
`readings` table), idempotent via UNIQUE(source, location, ts, metric).

DB path and table are configurable: defaults to a SEPARATE `instance/db/weather.db` so the
sensor hot tier stays clean and the weather lane is a self-contained file — the natural unit
to move during the future air-gapped backup transfer. Point `--db` at hot.db if you'd rather
co-locate.
"""

import logging
import sqlite3
from pathlib import Path

from .sources import WeatherReading

log = logging.getLogger("ha.weather")

_UNITS = {
    "temperature_c": "degC",
    "humidity_pct": "%",
    "pressure_hpa": "hPa",
    "pressure_msl_hpa": "hPa",
}


def _ddl(table: str) -> str:
    return f"""
    CREATE TABLE IF NOT EXISTS {table} (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        ts        TEXT    NOT NULL,
        source    TEXT    NOT NULL,
        location  TEXT    NOT NULL,
        metric    TEXT    NOT NULL,
        value     REAL    NOT NULL,
        unit      TEXT    NOT NULL,
        schema_v  INTEGER NOT NULL DEFAULT 1
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_unique
        ON {table} (source, location, ts, metric);
    CREATE INDEX IF NOT EXISTS idx_{table}_ts ON {table} (ts);
    """


class WeatherStore:
    def __init__(self, db_path: Path, table: str = "weather"):
        self.db_path = Path(db_path)
        self.table = table
        conn = self._conn()
        try:
            conn.executescript(_ddl(table))
            conn.commit()
        finally:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def store(self, reading: WeatherReading) -> int:
        """Insert one reading's metrics. Idempotent — returns rows actually added (new)."""
        if not reading.ts:
            log.warning("reading has no timestamp; skipping")
            return 0
        rows = [
            (reading.ts, reading.source, reading.location, metric, float(value),
             _UNITS.get(metric, ""), 1)
            for metric, value in reading.metrics.items()
        ]
        if not rows:
            return 0
        conn = self._conn()
        try:
            before = conn.total_changes
            conn.executemany(
                f"INSERT OR IGNORE INTO {self.table} "
                "(ts, source, location, metric, value, unit, schema_v) VALUES (?,?,?,?,?,?,?)",
                rows,
            )
            conn.commit()
            return conn.total_changes - before
        finally:
            conn.close()
