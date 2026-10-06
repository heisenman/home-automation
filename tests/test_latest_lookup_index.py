"""viewmodel._latest must SEEK, not scan (ha-2 2026-10-06: ~150 full per-device scans per PWA poll burned
~1.7 cores in ha-api). Guards that the writer's schema gives _latest's exact query an index on all three
equality columns + ts, on a db the writer itself opened."""
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from server.storage import writer  # noqa: E402

_LATEST_Q = ("SELECT value, ts FROM readings WHERE device_id=? AND metric=? AND authoritative=? "
             "ORDER BY ts DESC LIMIT 1")


def test_latest_query_uses_full_seek_index():
    with tempfile.TemporaryDirectory() as d:
        conn = writer._open_db(Path(d) / "hot.db")
        plan = " ".join(r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + _LATEST_Q, ("d", "m", 1)))
        conn.close()
    assert "idx_readings_latest" in plan and "authoritative=?" in plan, plan
    assert "TEMP B-TREE" not in plan, plan          # ORDER BY ts served by the index, no sort


if __name__ == "__main__":
    test_latest_query_uses_full_seek_index()
    print("ok")
