"""Tests for the writer's authoritative flag (server/storage/writer.py) — device self-reports (e.g. the
dehumidifier's onboard RH) must be ingested but marked non-authoritative so they don't pollute area truth."""
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.storage import writer as W  # noqa: E402
from tests._harness import run_module  # noqa: E402

METER = {"schema": 1, "device_id": "meter_pro_living_room", "device_type": "switchbot_meter",
         "area": "living_room", "transport": "ble-adv", "ts": "2026-06-22T10:00:00Z",
         "metrics": {"humidity_pct": 43.0, "temperature_c": 22.1}}
DEHUM = {"schema": 1, "device_id": "dehumidifier_office", "device_type": "dehumidifier",
         "area": "living_room", "transport": "midea-lan", "ts": "2026-06-22T10:00:05Z",
         "metrics": {"humidity_pct": 30.0}, "meta": {"authoritative": False}}


def _flag(conn, device_id):
    return conn.execute("SELECT authoritative FROM readings WHERE device_id=? LIMIT 1",
                        (device_id,)).fetchone()[0]


def test_trusted_meter_is_authoritative():
    with tempfile.TemporaryDirectory() as tmp:
        conn = W._open_db(Path(tmp) / "hot.db")
        W._insert_readings(conn, METER)
        assert _flag(conn, "meter_pro_living_room") == 1


def test_device_self_report_is_non_authoritative():
    with tempfile.TemporaryDirectory() as tmp:
        conn = W._open_db(Path(tmp) / "hot.db")
        W._insert_readings(conn, DEHUM)
        assert _flag(conn, "dehumidifier_office") == 0           # ingested, but flagged


def test_migration_adds_column_and_demotes_self_reports():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "hot.db"
        # build an OLD-schema readings table (no authoritative column) with a midea-lan row already in it
        old = sqlite3.connect(str(db))
        old.execute("""CREATE TABLE readings (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, device_id
            TEXT, device_type TEXT, area TEXT, transport TEXT, metric TEXT, value REAL, unit TEXT,
            schema_v INTEGER)""")
        old.execute("""INSERT INTO readings(ts,device_id,device_type,area,transport,metric,value,unit,
            schema_v) VALUES('t','dehumidifier_office','dehumidifier','living_room','midea-lan',
            'humidity_pct',30.0,'%',1)""")
        old.execute("""INSERT INTO readings(ts,device_id,device_type,area,transport,metric,value,unit,
            schema_v) VALUES('t','meter_pro_living_room','switchbot_meter','living_room','ble-adv',
            'humidity_pct',43.0,'%',1)""")
        old.commit()
        old.close()
        conn = W._open_db(db)                                    # triggers _migrate
        cols = {r[1] for r in conn.execute("PRAGMA table_info(readings)")}
        assert "authoritative" in cols
        assert _flag(conn, "dehumidifier_office") == 0           # pre-existing self-report demoted
        assert _flag(conn, "meter_pro_living_room") == 1         # trusted row stays authoritative


if __name__ == "__main__":
    run_module(globals())
