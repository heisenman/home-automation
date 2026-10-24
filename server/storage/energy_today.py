#!/usr/bin/env python3
"""energy_today.py — derive `energy_today_kwh` (kWh since the house's local midnight) for meters that report
only a lifetime `energy_kwh` (the PG03 plug firmware, ADR-0042). Tasmota S31s compute "Today" on the device
and publish it; our plugs stay dumb (ADR-0001) and the dictator, which owns the house timezone, derives it.

Today = the sum of the POSITIVE steps of the lifetime counter since local midnight. A step down is a counter
reset (the plug saves its total every 15 min, so a reboot can rewind up to 15 min of it): it adds nothing and
the count continues from the new value — never a negative day.

Live: storage/writer.py calls EnergyToday.add() for each payload with energy_kwh but no energy_today_kwh; the
derived point carries the source reading's ts, so it is deterministic and both stacks write the same rows.
One-time:  python3 server/storage/energy_today.py --backfill [--db instance/db/hot.db]   (INSERT OR IGNORE)
"""
import argparse
import calendar
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[2]
TZ_REFRESH_S = 600


def _epoch(ts: str) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")))
    except (TypeError, ValueError):
        return None


def house_tz(control_db: Path) -> str:
    """The house `timezone` setting (PWA); HOUSE_TZ_DEFAULT if unset/bad/unreadable."""
    from server.control.automation import HOUSE_TZ_DEFAULT, valid_tz
    try:
        if control_db.exists():
            from server.control import control_store as store
            cc = sqlite3.connect(f"file:{control_db}?mode=ro", uri=True)
            try:
                tz = store.get_setting(cc, "timezone")
            finally:
                cc.close()
            if valid_tz(tz):
                return tz
    except Exception:
        pass
    return HOUSE_TZ_DEFAULT


class EnergyToday:
    """Per-device running total. State is rebuilt from hot.db on first sight of a device (writer restart)."""

    def __init__(self, conn: sqlite3.Connection, tz_fn):
        self._conn, self._tz_fn = conn, tz_fn
        self._tz, self._tz_at = None, 0.0
        self._state: dict[str, tuple[str, float, float, float]] = {}   # device -> (day, today, last_total, ts)

    def _zone(self) -> ZoneInfo:
        now = time.monotonic()
        if self._tz is None or now - self._tz_at > TZ_REFRESH_S:
            self._tz, self._tz_at = ZoneInfo(self._tz_fn()), now
        return self._tz

    def _load(self, device_id: str, before_ts: str, zone: ZoneInfo):
        r = self._conn.execute("SELECT ts, value FROM readings WHERE device_id=? AND metric='energy_kwh' AND ts<? "
                               "ORDER BY ts DESC LIMIT 1", (device_id, before_ts)).fetchone()
        if r is None or _epoch(r[0]) is None:
            return None
        ep = _epoch(r[0])
        day = datetime.fromtimestamp(ep, zone).date().isoformat()
        t = self._conn.execute("SELECT value FROM readings WHERE device_id=? AND metric='energy_today_kwh' "
                               "AND ts=?", (device_id, r[0])).fetchone()
        return day, (t[0] if t else 0.0), float(r[1]), ep

    def add(self, device_id: str, ts: str, total_kwh: float) -> float | None:
        """energy_today_kwh for this reading, or None (bad ts, or older than the last one seen)."""
        ep = _epoch(ts)
        if ep is None:
            return None
        zone = self._zone()
        day = datetime.fromtimestamp(ep, zone).date().isoformat()
        st = self._state.get(device_id) or self._load(device_id, ts, zone)
        if st is None:
            today = 0.0
        else:
            sday, stoday, slast, sep = st
            if ep < sep:
                return None                               # out of order: never rewrite the running total
            step = total_kwh - slast
            step = step if step > 0 else 0.0               # counter reset (reboot rewound it): adds nothing
            today = (stoday if sday == day else 0.0) + step
        self._state[device_id] = (day, today, float(total_kwh), ep)
        return round(today, 6)


def backfill(db: Path, control_db: Path) -> int:
    """Walk each lifetime-only meter's whole energy_kwh history in order. A device needs it when it has fewer
    energy_today_kwh rows than energy_kwh rows (an S31 publishes both in every message, so it never qualifies)."""
    conn = sqlite3.connect(str(db))
    tz = house_tz(control_db)
    devs = [d for (d,) in conn.execute(
        "SELECT device_id FROM readings WHERE metric IN ('energy_kwh','energy_today_kwh') GROUP BY device_id "
        "HAVING SUM(metric='energy_today_kwh') < SUM(metric='energy_kwh')")]
    n = 0
    for did in devs:
        et = EnergyToday(conn, lambda: tz)                 # fresh state: the first row finds nothing earlier
        rows = conn.execute("SELECT ts, device_type, area, transport, value, schema_v, authoritative FROM readings "
                            "WHERE device_id=? AND metric='energy_kwh' ORDER BY ts", (did,)).fetchall()
        for ts, dtype, area, transport, v, schema_v, auth in rows:
            today = et.add(did, ts, v)
            if today is None:
                continue
            n += conn.execute("INSERT OR IGNORE INTO readings (ts, device_id, device_type, area, transport, metric, "
                              "value, unit, schema_v, authoritative) VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (ts, did, dtype, area, transport, "energy_today_kwh", today, "kWh", schema_v,
                               auth)).rowcount
        conn.commit()
        print(f"{did}: {len(rows)} energy_kwh readings walked")
    conn.close()
    return n


def main() -> None:
    sys.path.insert(0, str(REPO))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backfill", action="store_true", required=True)
    ap.add_argument("--db", type=Path, default=REPO / "instance/db/hot.db")
    ap.add_argument("--control-db", type=Path, default=REPO / "instance/db/control.db")
    a = ap.parse_args()
    print(f"inserted {backfill(a.db, a.control_db)} rows")


if __name__ == "__main__":
    main()
