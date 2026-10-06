"""purge_frames: a garbage FRAME (every metric at the marked ts) leaves hot.db, parquet and the device's
rungs; real frames, other devices, and other devices' rungs are untouched."""
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from server.maintenance import purge_frames as PF  # noqa: E402
from server.storage import rollup  # noqa: E402
from tests._harness import run_module  # noqa: E402

DEV = "dehumidifier_living_room"
NOW = rollup.epoch_of("2026-10-06T18:00:00Z")
GOOD_OLD, BAD_OLD = "2026-09-10T10:00:10Z", "2026-09-10T10:00:40Z"     # parquet tier, same minute
GOOD_NEW, BAD_NEW = "2026-10-06T17:00:10Z", "2026-10-06T17:00:40Z"     # hot tier, same minute


def _frame(ts, good):
    return [(ts, DEV, "mode", 2.0 if good else 0.0), (ts, DEV, "humidity_pct", 31.0 if good else 1.0)]


def _setup(d: Path):
    hot = sqlite3.connect(d / "hot.db")
    hot.execute("CREATE TABLE readings (ts TEXT, device_id TEXT, metric TEXT, value REAL)")
    rows = _frame(GOOD_NEW, True) + _frame(BAD_NEW, False) + [(BAD_NEW, "other", "mode", 0.0)]
    hot.executemany("INSERT INTO readings VALUES (?,?,?,?)", rows)
    hot.commit()
    old = _frame(GOOD_OLD, True) + _frame(BAD_OLD, False) + [(BAD_OLD, "other", "humidity_pct", 50.0)]
    (d / "pq").mkdir()
    pq.write_table(pa.table({"ts": [r[0] for r in old], "device_id": [r[1] for r in old],
                             "metric": [r[2] for r in old], "value": [r[3] for r in old]}),
                   d / "pq" / "2026-09.parquet")
    rc = sqlite3.connect(d / "rungs.db")
    rollup.ensure_schema(rc)
    allraw = rows + old
    rc.executemany(rollup._UPSERT, [("1min", dv, m, b, *a) for (dv, m, b), a in rollup.aggregate_raw(allraw).items()])
    for dest, src in rollup.CASCADE:
        rollup.cascade(rc, dest, src, 0, NOW + 1)
    rc.close()
    hot.close()


def _run(d, dry):
    return PF.run(DEV, "mode", 0.0, dry_run=dry, hot_db=d / "hot.db", rung_db=d / "rungs.db",
                  parquet_glob=str(d / "pq" / "*.parquet"), backup_root=d / "bk", now_epoch=NOW,
                  rebuild_manifest=lambda dry: "test")


def test_dry_run_counts_and_writes_nothing():
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        _setup(d)
        r = _run(d, True)
        assert r["hot"] == 2 and r["parquet"] == {"2026-09.parquet": 2}
        assert sqlite3.connect(d / "hot.db").execute("SELECT count(*) FROM readings").fetchone()[0] == 5
        assert not (d / "bk").exists()


def test_purge_removes_frames_everywhere_and_rebuilds_device_rungs():
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        _setup(d)
        r = _run(d, False)
        hot = sqlite3.connect(d / "hot.db")
        assert sorted(hot.execute("SELECT ts, device_id FROM readings")) == \
            sorted([(BAD_NEW, "other"), (GOOD_NEW, DEV), (GOOD_NEW, DEV)])  # other device's mode 0 kept
        tb = pq.ParquetFile(d / "pq" / "2026-09.parquet").read()
        assert sorted(zip(tb.column("ts").to_pylist(), tb.column("device_id").to_pylist())) == \
            sorted([(BAD_OLD, "other"), (GOOD_OLD, DEV), (GOOD_OLD, DEV)])
        rc = sqlite3.connect(d / "rungs.db")
        # no garbage left in ANY of the device's rungs; vcount = the one good sample per bucket
        assert rc.execute("SELECT count(*) FROM rung WHERE device_id=? AND vmin IN (0, 1)", (DEV,)).fetchone()[0] == 0
        assert rc.execute("SELECT DISTINCT vcount FROM rung WHERE device_id=? AND res IN ('1min','1hour')",
                          (DEV,)).fetchall() == [(1,)]
        # 1min retention (7d): the September minute is not re-created; its 1hour is
        assert rc.execute("SELECT count(*) FROM rung WHERE device_id=? AND res='1min' AND bucket_start < ?",
                          (DEV, NOW - 7 * 86400)).fetchone()[0] == 0
        assert rc.execute("SELECT count(*) FROM rung WHERE device_id=? AND res='1hour' AND bucket_start < ?",
                          (DEV, NOW - 7 * 86400)).fetchone()[0] == 2
        assert rc.execute("SELECT count(*) FROM rung WHERE device_id='other'").fetchone()[0] > 0   # untouched
        assert (Path(r["backup"]) / "hot.db").exists() and (Path(r["backup"]) / "2026-09.parquet").exists()


if __name__ == "__main__":
    run_module(globals())
