"""End-to-end compactor run, invoked EXACTLY as systemd invokes it.

The 2026-08-02 failure was invisible to every other kind of test: `compactor.py` imports fine under
pytest, fine under `python3 -m`, fine from a REPL — and raised ModuleNotFoundError only under
`ExecStart=venv/bin/python3 server/storage/compactor.py`, because a script-path invocation puts
server/storage/ on sys.path instead of the repo root. So this test shells out the same way the unit does,
against a throwaway database, rather than importing the module.

It also pins the ORDERING that made the failure expensive: the DELETE from `readings` had already
committed when the import blew up, so the latest_readings prune and the WAL checkpoint were skipped and
the operator was left with a half-finished compaction reported as a crash.
"""
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from server.storage import latest_cache as LC  # noqa: E402
from tests.test_latest_cache import DDL_READINGS  # noqa: E402  (one schema, one place)

VENV_PY = REPO / "venv/bin/python3"


def _seed(db: Path, old_ts: str, fresh_ts: str):
    c = sqlite3.connect(db)
    c.executescript(DDL_READINGS)
    LC.ensure(c)
    rows = [(old_ts, "gas_old", "voc_index", 40.0),      # compactable -> prunes out of the cache
            (fresh_ts, "gas_new", "voc_index", 55.0)]    # after the cutoff -> stays
    for ts, did, metric, val in rows:
        c.execute("INSERT INTO readings (ts,device_id,device_type,area,transport,metric,value,unit,"
                  "schema_v,authoritative) VALUES (?,?,'sgp41_gas','a','i2c',?,?,'',1,1)",
                  (ts, did, metric, val))
    c.commit()
    c.close()


def _cache_ids(db: Path):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {r[0] for r in c.execute("SELECT device_id FROM latest_readings")}
    finally:
        c.close()


@pytest.mark.skipif(not VENV_PY.exists(), reason="repo venv (with duckdb/pyarrow) not present")
def test_compactor_runs_clean_as_systemd_invokes_it(tmp_path):
    db, pq = tmp_path / "hot.db", tmp_path / "parquet"
    pq.mkdir()
    # Cutoff is "yesterday 00:00 UTC", so anchor the seed well either side of it.
    _seed(db, old_ts="2020-01-01T00:00:00Z", fresh_ts="2999-01-01T00:00:00Z")
    assert _cache_ids(db) == {"gas_old", "gas_new"}

    r = subprocess.run(
        [str(VENV_PY), "server/storage/compactor.py",           # BY PATH — the systemd form
         "--db", str(db), "--parquet-dir", str(pq), "--log-level", "INFO"],
        cwd=REPO, capture_output=True, text=True, timeout=180)

    assert r.returncode == 0, f"compactor failed as systemd runs it:\n{r.stderr}"
    assert "ModuleNotFoundError" not in r.stderr

    # The compaction actually completed rather than dying after the destructive step.
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert c.execute("SELECT COUNT(*) FROM readings WHERE device_id='gas_old'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM readings WHERE device_id='gas_new'").fetchone()[0] == 1
    finally:
        c.close()
    # ...and the cache was pruned in step with it — the step that was being skipped in production.
    assert _cache_ids(db) == {"gas_new"}
