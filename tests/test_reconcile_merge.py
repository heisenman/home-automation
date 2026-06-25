"""ADR-0016 reconcile — validates the core merge logic the bash script runs (bidirectional, idempotent,
windowed). The SSH/scp transport is validated live on the cluster; here we prove the SQL converges."""
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._harness import run_module  # noqa: E402

_DDL = """
CREATE TABLE readings (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, device_id TEXT NOT NULL,
  device_type TEXT NOT NULL, area TEXT NOT NULL, transport TEXT NOT NULL, metric TEXT NOT NULL,
  value REAL NOT NULL, unit TEXT NOT NULL, schema_v INTEGER NOT NULL DEFAULT 1,
  authoritative INTEGER NOT NULL DEFAULT 1);
CREATE UNIQUE INDEX idx_readings_unique ON readings (device_id, ts, metric);
"""
_COLS = "ts,device_id,device_type,area,transport,metric,value,unit,schema_v,authoritative"


def _db(path, rows):
    c = sqlite3.connect(path)
    c.executescript(_DDL)
    c.executemany(f"INSERT INTO readings({_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    c.commit()
    return c


def _row(ts, dev, metric, val):
    return (ts, dev, "switchbot_meter", "area", "ble-adv", metric, val, "C", 1, 1)


def _keys(conn):
    return sorted(conn.execute("SELECT device_id, ts, metric FROM readings").fetchall())


def _merge(dst_path, snap_path):
    """Mirror do_merge(): ATTACH snap + INSERT OR IGNORE; return rows added."""
    c = sqlite3.connect(dst_path)
    before = c.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    c.execute(f"ATTACH '{snap_path}' AS s")
    c.execute(f"INSERT OR IGNORE INTO readings({_COLS}) SELECT {_COLS} FROM s.readings")
    c.commit()
    after = c.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    c.close()
    return after - before


def test_bidirectional_merge_converges_to_union():
    with tempfile.TemporaryDirectory() as d:
        a, b = f"{d}/a.db", f"{d}/b.db"
        # A's reign + a shared row; B's reign + the same shared row (overlap)
        shared = _row("2026-06-25T00:00:00Z", "meter_kitchen", "temperature_c", 21.0)
        ca = _db(a, [shared, _row("2026-06-25T01:00:00Z", "meter_kitchen", "temperature_c", 22.0)])
        cb = _db(b, [shared, _row("2026-06-25T02:00:00Z", "meter_attic", "humidity_pct", 40.0)])
        ca.close(); cb.close()
        # bidirectional: merge each into the other (snap == the peer db itself for the test)
        _merge(a, b)
        _merge(b, a)
        ca, cb = sqlite3.connect(a), sqlite3.connect(b)
        assert _keys(ca) == _keys(cb), "boxes did not converge"
        assert len(_keys(ca)) == 3, "shared row duplicated or a row was lost"
        ca.close(); cb.close()


def test_merge_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        a, b = f"{d}/a.db", f"{d}/b.db"
        ca = _db(a, [_row("2026-06-25T01:00:00Z", "m", "temperature_c", 1.0)])
        cb = _db(b, [_row("2026-06-25T02:00:00Z", "m", "temperature_c", 2.0)])
        ca.close(); cb.close()
        first = _merge(a, b)
        second = _merge(a, b)     # re-merge the same snapshot
        assert first == 1 and second == 0, (first, second)   # no dupes on re-run


def test_unique_key_blocks_dupe_not_distinct_metric():
    with tempfile.TemporaryDirectory() as d:
        a, b = f"{d}/a.db", f"{d}/b.db"
        # same (device,ts) different metric => distinct rows; same (device,ts,metric) => one wins
        ca = _db(a, [_row("2026-06-25T01:00:00Z", "m", "temperature_c", 1.0)])
        cb = _db(b, [_row("2026-06-25T01:00:00Z", "m", "humidity_pct", 50.0),
                     _row("2026-06-25T01:00:00Z", "m", "temperature_c", 9.9)])  # collides on (m,ts,temp)
        ca.close(); cb.close()
        added = _merge(a, b)
        ca = sqlite3.connect(a)
        assert added == 1                                  # only humidity_pct is new
        assert ca.execute("SELECT value FROM readings WHERE metric='temperature_c'").fetchone()[0] == 1.0
        ca.close()                                         # INSERT OR IGNORE keeps the incumbent


def test_script_export_merge_roundtrip_via_bash():
    """End-to-end through the actual script's --export/--merge primitives (not just mirrored SQL)."""
    script = Path(__file__).resolve().parents[1] / "failover" / "reconcile-history.sh"
    if not script.exists():
        return  # skip if not present
    with tempfile.TemporaryDirectory() as d:
        src, dst, snap = f"{d}/src.db", f"{d}/dst.db", f"{d}/w.snap"
        _db(src, [_row("2026-06-25T01:00:00Z", "m", "temperature_c", 5.0),
                  _row("2020-01-01T00:00:00Z", "m", "temperature_c", 1.0)]).close()  # old row < cutoff
        _db(dst, [_row("2026-06-25T02:00:00Z", "m", "humidity_pct", 30.0)]).close()
        import os
        env = {**os.environ, "HOT_DB": src}
        # export src's window (>= cutoff) -> snap
        subprocess.run(["bash", str(script), "--export", snap, "2026-06-24T00:00:00Z"], env=env, check=True)
        env["HOT_DB"] = dst
        out = subprocess.run(["bash", str(script), "--merge", snap], env=env, check=True,
                             capture_output=True, text=True)
        added = int(out.stdout.strip().splitlines()[-1])
        c = sqlite3.connect(dst)
        keys = _keys(c); c.close()
        assert added == 1, f"expected 1 windowed row merged, got {added}"   # the 2020 row excluded by window
        assert ("m", "2026-06-25T01:00:00Z", "temperature_c") in keys
        assert ("m", "2020-01-01T00:00:00Z", "temperature_c") not in keys   # below cutoff -> not shipped


if __name__ == "__main__":
    run_module(globals())
