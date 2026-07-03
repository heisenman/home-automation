"""server/maintenance/device_migrate.py — the codified device rename/retire procedure.

Covers the pure store operations (sqlite + parquet, rename & retire, idempotency, missing-table safety)
and the run_migration orchestration report (peer/MQTT side-effects disabled). The SSH peer step and the
retained-MQTT clear are thin wrappers verified on a real run, not unit-tested here."""
import sqlite3

from server.maintenance import device_migrate as D
from tests._harness import raises


def _hot(tmp_path, rows, area="h_bedroom"):
    p = tmp_path / "hot.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE readings(ts TEXT, device_id TEXT, metric TEXT, value REAL)")
    c.execute("CREATE TABLE device_last_seen(device_id TEXT, area TEXT, last_ts TEXT)")
    c.execute("CREATE TABLE summaries(device_id TEXT, d TEXT)")
    c.execute("CREATE TABLE unrelated(id INTEGER)")            # no device_id column -> must be skipped
    c.executemany("INSERT INTO readings VALUES(?,?,?,?)", rows)
    c.execute("INSERT INTO device_last_seen VALUES('old', ?, 't')", (area,))
    c.execute("INSERT INTO summaries VALUES('old','x')")
    c.commit(); c.close()
    return str(p)


# ── sqlite: rename / retire / idempotency / safety ────────────────────────────────────────────────────
def test_apply_sqlite_rename_and_idempotent(tmp_path):
    hot = _hot(tmp_path, [("t1", "old", "eco2", 1.0), ("t2", "old", "eco2", 2.0), ("t3", "keep", "x", 9)])
    out = D.apply_sqlite(hot, D.HOT_TABLES, "old", "new", retire=False, dry_run=False)
    assert out == {"readings": 2, "device_last_seen": 1, "summaries": 1}
    assert D.count_sqlite(hot, D.HOT_TABLES, "old") == 0
    assert D.count_sqlite(hot, D.HOT_TABLES, "new") == 4                       # 2 readings + last_seen + summary
    # unrelated rows untouched; re-run is a no-op (idempotent)
    again = D.apply_sqlite(hot, D.HOT_TABLES, "old", "new", retire=False, dry_run=False)
    assert sum(again.values()) == 0


def test_apply_sqlite_retire_deletes(tmp_path):
    hot = _hot(tmp_path, [("t1", "old", "eco2", 1.0), ("t2", "keep", "x", 9)])
    out = D.apply_sqlite(hot, D.HOT_TABLES, "old", None, retire=True, dry_run=False)
    assert out["readings"] == 1
    assert D.count_sqlite(hot, D.HOT_TABLES, "old") == 0
    assert D.count_sqlite(hot, D.HOT_TABLES, "keep") == 1


def test_apply_sqlite_dry_run_counts_but_no_write(tmp_path):
    hot = _hot(tmp_path, [("t1", "old", "eco2", 1.0)])
    out = D.apply_sqlite(hot, D.HOT_TABLES, "old", "new", retire=False, dry_run=True)
    assert out["readings"] == 1
    assert D.count_sqlite(hot, D.HOT_TABLES, "old") == 3                       # 1 reading + last_seen + summary, unchanged


def test_lookup_area(tmp_path):
    hot = _hot(tmp_path, [("t1", "old", "eco2", 1.0)], area="c_office")
    assert D.lookup_area(hot, "old") == "c_office"
    assert D.lookup_area(hot, "nope") is None


# ── parquet: rename / retire ──────────────────────────────────────────────────────────────────────────
def _parquet(tmp_path, ids):
    import pyarrow as pa
    import pyarrow.parquet as pq
    p = tmp_path / "year=2026" / "month=07"
    p.mkdir(parents=True)
    f = p / "2026-07.parquet"
    pq.write_table(pa.table({"ts": [f"t{i}" for i in range(len(ids))], "device_id": ids,
                             "metric": ["eco2"] * len(ids), "value": [1.0] * len(ids)}), str(f))
    return str(tmp_path / "year=*" / "month=*" / "*.parquet")


def test_apply_parquet_rename(tmp_path):
    import pyarrow.parquet as pq
    g = _parquet(tmp_path, ["old", "old", "keep"])
    out = D.apply_parquet(g, "old", "new", retire=False, dry_run=False)
    assert list(out.values()) == [2]
    import glob
    col = pq.ParquetFile(glob.glob(g)[0]).read(columns=["device_id"]).column("device_id").to_pylist()
    assert sorted(col) == ["keep", "new", "new"]


def test_apply_parquet_retire(tmp_path):
    import glob

    import pyarrow.parquet as pq
    g = _parquet(tmp_path, ["old", "keep", "old"])
    out = D.apply_parquet(g, "old", None, retire=True, dry_run=False)
    assert list(out.values()) == [2]
    col = pq.ParquetFile(glob.glob(g)[0]).read(columns=["device_id"]).column("device_id").to_pylist()
    assert col == ["keep"]


# ── orchestration report (peer + mqtt disabled) ───────────────────────────────────────────────────────
def test_run_migration_rename_report(tmp_path):
    hot = _hot(tmp_path, [("t1", "old", "eco2", 1.0), ("t2", "old", "tvoc", 2.0)])
    rep = D.run_migration("rename", "old", "new", hot_db=hot, rung_db=str(tmp_path / "none.db"),
                          parquet_glob=str(tmp_path / "none" / "*.parquet"),
                          do_peer=False, do_mqtt=False, backup=False, dry_run=False)
    assert rep["op"] == "rename" and rep["clean"] is True
    assert rep["hot"]["readings"] == 2
    assert rep["verify_old_id_local"]["hot"] == 0


def test_run_migration_rename_requires_new_id(tmp_path):
    hot = _hot(tmp_path, [("t1", "old", "eco2", 1.0)])
    with raises(ValueError):
        D.run_migration("rename", "old", None, hot_db=hot, do_peer=False, do_mqtt=False, backup=False)
