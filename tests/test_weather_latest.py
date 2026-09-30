"""Tests for the weather lane's latest-snapshot read (server.weather.store.latest_reading) — the
"current outdoor conditions" glance shown beside attic/crawlspace on the PWA map and the D1001 panel.

The failure worth guarding is showing the WRONG row as current: a future (forecast) hour presented as
a measurement, or metrics from two different timestamps stitched into one snapshot."""
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from server.weather.store import _ddl, latest_reading  # noqa: E402


def _db(rows):
    c = sqlite3.connect(":memory:")
    c.executescript(_ddl("weather"))
    c.executemany("INSERT INTO weather (ts,source,location,metric,value,unit) VALUES (?,?,?,?,?,'')", rows)
    return c


def test_empty_lane_is_none():
    assert latest_reading(_db([]), "weather", "2026-09-30T00:00:00Z") is None


def test_newest_snapshot_all_metrics():
    c = _db([("2026-09-29T23:00:00Z", "openmeteo", "home", "temperature_c", 18.0),
             ("2026-09-29T23:00:00Z", "openmeteo", "home", "humidity_pct", 70.0),
             ("2026-09-30T00:00:00Z", "openmeteo", "home", "temperature_c", 19.3),
             ("2026-09-30T00:00:00Z", "openmeteo", "home", "humidity_pct", 60.0)])
    s = latest_reading(c, "weather", "2026-09-30T00:30:00Z")
    assert s == {"ts": "2026-09-30T00:00:00Z", "source": "openmeteo", "location": "home",
                 "metrics": {"temperature_c": 19.3, "humidity_pct": 60.0}}


def test_future_rows_are_never_current():
    c = _db([("2026-09-30T00:00:00Z", "openmeteo", "home", "temperature_c", 19.3),
             ("2026-09-30T03:00:00Z", "openmeteo", "home", "temperature_c", 25.0)])   # forecast hour
    assert latest_reading(c, "weather", "2026-09-30T00:30:00Z")["metrics"] == {"temperature_c": 19.3}


def test_snapshot_does_not_mix_locations():
    c = _db([("2026-09-30T00:00:00Z", "openmeteo", "home", "temperature_c", 19.3),
             ("2026-09-30T00:00:00Z", "openmeteo", "zz_other", "temperature_c", 5.0)])
    s = latest_reading(c, "weather", "2026-09-30T00:30:00Z")
    assert s["location"] == "home" and s["metrics"] == {"temperature_c": 19.3}


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
    print("ok")
