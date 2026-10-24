"""energy_today_kwh derived from a lifetime-only energy_kwh counter (PG03 plugs, ADR-0042): resets at the
HOUSE's local midnight, a counter rewind (reboot) adds nothing, survives a writer restart, backfills."""
import sqlite3

from server.storage import writer as W
from server.storage.energy_today import EnergyToday, backfill

TZ = "America/Los_Angeles"            # PDT = UTC-7 in October: local midnight = 07:00Z


def _db(tmp_path):
    return W._open_db(tmp_path / "hot.db")


def _put(conn, et, ts, kwh, dev="plug_hall"):
    p = {"schema": 1, "device_id": dev, "device_type": "energy_meter", "area": "hall", "transport": "uart",
         "ts": ts, "metrics": {"energy_kwh": kwh, "power_w": 1.0}}
    W._derive_energy_today(et, p)
    W._insert_readings(conn, p)
    return p["metrics"].get("energy_today_kwh")


def test_counts_up_and_resets_at_house_midnight_not_utc(tmp_path):
    c = _db(tmp_path)
    et = EnergyToday(c, lambda: TZ)
    assert _put(c, et, "2026-10-23T23:00:00Z", 10.0) == 0.0        # first ever reading: nothing yet today
    assert _put(c, et, "2026-10-24T00:30:00Z", 10.5) == 0.5        # UTC midnight passed — same house day
    assert _put(c, et, "2026-10-24T06:59:00Z", 11.0) == 1.0
    assert _put(c, et, "2026-10-24T07:01:00Z", 11.25) == 0.25      # house midnight: new day


def test_counter_rewind_adds_nothing_then_counting_resumes(tmp_path):
    c = _db(tmp_path)
    et = EnergyToday(c, lambda: TZ)
    _put(c, et, "2026-10-24T08:00:00Z", 5.0)
    assert _put(c, et, "2026-10-24T09:00:00Z", 5.4) == 0.4
    assert _put(c, et, "2026-10-24T09:01:00Z", 5.3) == 0.4         # reboot rewound the saved total
    assert _put(c, et, "2026-10-24T10:00:00Z", 5.5) == 0.6


def test_out_of_order_reading_is_not_derived(tmp_path):
    c = _db(tmp_path)
    et = EnergyToday(c, lambda: TZ)
    _put(c, et, "2026-10-24T08:00:00Z", 5.0)
    _put(c, et, "2026-10-24T09:00:00Z", 5.4)
    assert _put(c, et, "2026-10-24T08:30:00Z", 5.2) is None


def test_writer_restart_resumes_from_stored_rows(tmp_path):
    c = _db(tmp_path)
    _put(c, EnergyToday(c, lambda: TZ), "2026-10-24T08:00:00Z", 5.0)
    _put(c, EnergyToday(c, lambda: TZ), "2026-10-24T09:00:00Z", 5.4)    # a fresh instance each time
    assert _put(c, EnergyToday(c, lambda: TZ), "2026-10-24T10:00:00Z", 5.5) == 0.5


def test_a_meter_that_reports_today_itself_is_left_alone(tmp_path):
    c = _db(tmp_path)
    p = {"device_id": "dehum_pm", "ts": "2026-10-24T08:00:00Z", "metrics": {"energy_kwh": 9.0,
                                                                           "energy_today_kwh": 0.7}}
    W._derive_energy_today(EnergyToday(c, lambda: TZ), p)
    assert p["metrics"]["energy_today_kwh"] == 0.7


def test_backfill_walks_history_and_skips_s31s(tmp_path):
    c = _db(tmp_path)
    for ts, kwh in (("2026-10-24T06:00:00Z", 1.0), ("2026-10-24T06:30:00Z", 1.2), ("2026-10-24T07:30:00Z", 1.5)):
        W._insert_readings(c, {"device_id": "plug_hall", "device_type": "energy_meter", "area": "hall",
                               "ts": ts, "metrics": {"energy_kwh": kwh}})
    W._insert_readings(c, {"device_id": "dehum_pm", "device_type": "energy_meter", "area": "attic",
                           "ts": "2026-10-24T07:30:00Z", "metrics": {"energy_kwh": 9.0, "energy_today_kwh": 0.7}})
    c.close()
    assert backfill(tmp_path / "hot.db", tmp_path / "no-control.db") == 3
    assert backfill(tmp_path / "hot.db", tmp_path / "no-control.db") == 0              # idempotent
    c = sqlite3.connect(tmp_path / "hot.db")
    got = c.execute("SELECT device_id, ts, round(value,3) FROM readings WHERE metric='energy_today_kwh' "
                    "ORDER BY device_id, ts").fetchall()
    assert got == [("dehum_pm", "2026-10-24T07:30:00Z", 0.7), ("plug_hall", "2026-10-24T06:00:00Z", 0.0),
                   ("plug_hall", "2026-10-24T06:30:00Z", 0.2), ("plug_hall", "2026-10-24T07:30:00Z", 0.3)]


def test_backfill_corrects_rows_a_live_writer_started_at_zero(tmp_path):
    c = _db(tmp_path)
    for ts, kwh in (("2026-10-24T08:00:00Z", 1.0), ("2026-10-24T09:00:00Z", 1.5)):
        W._insert_readings(c, {"device_id": "plug_hall", "device_type": "energy_meter", "area": "hall",
                               "ts": ts, "metrics": {"energy_kwh": kwh}})
    assert _put(c, EnergyToday(c, lambda: TZ), "2026-10-24T10:00:00Z", 1.75) == 0.25   # live: no history yet
    c.close()
    backfill(tmp_path / "hot.db", tmp_path / "no-control.db", ["plug_hall"])
    c = sqlite3.connect(tmp_path / "hot.db")
    assert c.execute("SELECT value FROM readings WHERE metric='energy_today_kwh' AND ts='2026-10-24T10:00:00Z'"
                     ).fetchone()[0] == 0.75
