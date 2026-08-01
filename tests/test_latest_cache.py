"""Tests for the materialized latest-reading cache (board sensors-query-unbounded).

The cache replaces an O(rows) GROUP BY that ran on every /api/v1/sensors. The risk in that trade is
being FAST AND WRONG, so these tests are mostly about the ways it could silently disagree with the query
it replaces — above all out-of-order inserts, which are normal here (history recovery replays a meter's
on-device log; reconcile merges a peer's older window).
"""
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from server.storage import latest_cache as LC  # noqa: E402

DDL_READINGS = """
CREATE TABLE readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, device_id TEXT NOT NULL, device_type TEXT NOT NULL, area TEXT NOT NULL,
    transport TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL, unit TEXT NOT NULL,
    schema_v INTEGER NOT NULL DEFAULT 1, authoritative INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX idx_readings_unique ON readings (device_id, ts, metric);
"""


def _db():
    c = sqlite3.connect(":memory:")
    c.executescript(DDL_READINGS)
    return c


def _ins(c, did, metric, value, ts, auth=1):
    c.execute("INSERT OR IGNORE INTO readings (ts,device_id,device_type,area,transport,metric,value,unit,"
              "schema_v,authoritative) VALUES (?,?,'t','a','x',?,?,'',1,?)",
              (ts, did, metric, value, auth))
    c.commit()


def _cache(c):
    return dict(((d, m), (v, t)) for d, m, v, t in
                c.execute("SELECT device_id, metric, value, ts FROM latest_readings"))


def test_trigger_tracks_the_newest_reading():
    c = _db(); LC.ensure(c)
    _ins(c, "m1", "temperature_c", 20.0, "2026-08-01T10:00:00Z")
    _ins(c, "m1", "temperature_c", 21.0, "2026-08-01T11:00:00Z")
    assert _cache(c)[("m1", "temperature_c")] == (21.0, "2026-08-01T11:00:00Z")


def test_backfill_must_not_overwrite_a_newer_value():
    """THE load-bearing guard. History recovery replays a meter's on-device log with BACKDATED
    timestamps (2862 such rows landed on ha-2 on 2026-08-01). Without the ts comparison this would
    make the whole PWA show week-old readings."""
    c = _db(); LC.ensure(c)
    _ins(c, "m1", "temperature_c", 21.0, "2026-08-01T11:00:00Z")
    _ins(c, "m1", "temperature_c", 5.0, "2026-07-25T03:00:00Z")      # a backfilled historical row
    assert _cache(c)[("m1", "temperature_c")] == (21.0, "2026-08-01T11:00:00Z")


def test_out_of_order_burst_still_lands_on_the_max():
    c = _db(); LC.ensure(c)
    for ts, v in [("2026-08-01T10:00:00Z", 1.0), ("2026-08-01T12:00:00Z", 3.0),
                  ("2026-08-01T09:00:00Z", 0.5), ("2026-08-01T11:00:00Z", 2.0)]:
        _ins(c, "m1", "humidity_pct", v, ts)
    assert _cache(c)[("m1", "humidity_pct")] == (3.0, "2026-08-01T12:00:00Z")


def test_self_reports_are_excluded():
    """authoritative=0 (e.g. the dehumidifier's own RH) never belongs in the sensor view."""
    c = _db(); LC.ensure(c)
    _ins(c, "dehum", "humidity_pct", 55.0, "2026-08-01T10:00:00Z", auth=0)
    assert ("dehum", "humidity_pct") not in _cache(c)


def test_metrics_are_tracked_independently():
    c = _db(); LC.ensure(c)
    _ins(c, "m1", "temperature_c", 20.0, "2026-08-01T10:00:00Z")
    _ins(c, "m1", "humidity_pct", 40.0, "2026-08-01T09:00:00Z")
    got = _cache(c)
    assert got[("m1", "temperature_c")] == (20.0, "2026-08-01T10:00:00Z")
    assert got[("m1", "humidity_pct")] == (40.0, "2026-08-01T09:00:00Z")


def test_duplicate_insert_is_ignored_and_harmless():
    """Reconcile re-merges overlapping windows constantly; an ignored INSERT must not fire the trigger."""
    c = _db(); LC.ensure(c)
    _ins(c, "m1", "temperature_c", 20.0, "2026-08-01T10:00:00Z")
    _ins(c, "m1", "temperature_c", 99.0, "2026-08-01T10:00:00Z")     # same key -> IGNOREd
    assert _cache(c)[("m1", "temperature_c")] == (20.0, "2026-08-01T10:00:00Z")


def test_backfill_of_preexisting_rows_on_first_ensure():
    """Rows written BEFORE the migration must be picked up, or the cache would answer with a partial set."""
    c = _db()
    _ins(c, "m1", "temperature_c", 20.0, "2026-08-01T10:00:00Z")
    _ins(c, "m2", "temperature_c", 22.0, "2026-08-01T10:00:00Z")
    LC.ensure(c)
    assert len(_cache(c)) == 2


def test_matches_the_query_it_replaces():
    """Direct parity with the source derivation — the property that actually matters."""
    c = _db(); LC.ensure(c)
    import random
    random.seed(7)
    stamps = [f"2026-08-01T{h:02d}:00:00Z" for h in range(24)]
    for dev in ("m1", "m2", "m3"):
        for metric in ("temperature_c", "humidity_pct"):
            for ts in random.sample(stamps, 10):                     # deliberately out of order
                _ins(c, dev, metric, random.random() * 30, ts)
    ndiff, sample = LC.compare_with_source(c)
    assert ndiff == 0, sample


def test_prune_matches_post_compaction_visibility():
    """The compactor drops rows < cutoff; a device whose newest reading went with them must disappear
    from the cache too, exactly as it disappears from the un-cached query."""
    c = _db(); LC.ensure(c)
    _ins(c, "live", "temperature_c", 20.0, "2026-08-01T10:00:00Z")
    _ins(c, "dead", "temperature_c", 20.0, "2026-07-20T10:00:00Z")
    cutoff = "2026-07-31T00:00:00Z"
    c.execute("DELETE FROM readings WHERE ts < ?", (cutoff,)); c.commit()
    assert LC.prune(c, cutoff) == 1
    assert set(_cache(c)) == {("live", "temperature_c")}
    ndiff, sample = LC.compare_with_source(c)
    assert ndiff == 0, sample


def test_rebuild_recovers_from_a_bulk_device_id_update():
    """device_migrate/device_relocate UPDATE device_id in bulk — invisible to an INSERT trigger. The
    cache goes stale and only an explicit rebuild fixes it; this pins that contract."""
    c = _db(); LC.ensure(c)
    _ins(c, "old_id", "temperature_c", 20.0, "2026-08-01T10:00:00Z")
    c.execute("UPDATE readings SET device_id='new_id'"); c.commit()
    assert ("old_id", "temperature_c") in _cache(c)                  # stale, as expected
    assert LC.compare_with_source(c)[0] > 0                          # and detectable
    LC.rebuild(c)
    assert set(_cache(c)) == {("new_id", "temperature_c")}
    assert LC.compare_with_source(c)[0] == 0


def test_is_usable_requires_table_trigger_and_content():
    c = _db()
    assert LC.is_usable(c) is False                                  # nothing yet
    _ins(c, "m1", "temperature_c", 20.0, "2026-08-01T10:00:00Z")
    LC.ensure(c)
    assert LC.is_usable(c) is True
    c.execute("DELETE FROM latest_readings"); c.commit()
    assert LC.is_usable(c) is False                                  # empty -> fall back, never answer []


def test_is_usable_false_when_trigger_missing():
    """A table without its trigger would freeze at its backfilled values and go quietly stale."""
    c = _db(); LC.ensure(c)
    _ins(c, "m1", "temperature_c", 20.0, "2026-08-01T10:00:00Z")
    c.execute("DROP TRIGGER trg_readings_latest_ins"); c.commit()
    assert LC.is_usable(c) is False


def test_ensure_is_idempotent():
    c = _db(); LC.ensure(c)
    _ins(c, "m1", "temperature_c", 20.0, "2026-08-01T10:00:00Z")
    for _ in range(3):
        LC.ensure(c)
    assert len(_cache(c)) == 1


def test_build_sensor_list_agrees_cached_vs_fallback():
    """End-to-end: the viewmodel must produce identical output on both paths."""
    from server.api import viewmodel as V
    c = _db()
    c.executescript("CREATE TABLE device_last_seen (device_id TEXT PRIMARY KEY, device_type TEXT,"
                    " area TEXT, last_ts TEXT, last_rssi INTEGER);")
    c.execute("INSERT INTO device_last_seen VALUES ('m1','switchbot_meter','kitchen','x',NULL)")
    for ts, v in [("2026-08-01T10:00:00Z", 20.0), ("2026-08-01T12:00:00Z", 22.0),
                  ("2026-08-01T09:00:00Z", 19.0)]:
        _ins(c, "m1", "temperature_c", v, ts)
    now = 1785600000.0
    uncached = V.build_sensor_list(c, now)
    LC.ensure(c)
    cached = V.build_sensor_list(c, now)
    assert cached == uncached
    assert cached[0]["metrics"]["temperature_c"] == 22.0
