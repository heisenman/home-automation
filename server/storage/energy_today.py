#!/usr/bin/env python3
"""energy_today.py — derive `energy_today_kwh` (kWh since the house's local midnight) for meters that report
only a lifetime `energy_kwh` (the PG03 plug firmware, ADR-0042). Tasmota S31s compute "Today" on the device
and publish it; our plugs stay dumb (ADR-0001) and the dictator, which owns the house timezone, derives it.

Today = the sum of the POSITIVE steps of the lifetime counter since local midnight. A step down is a counter
reset (the plug saves its total every 15 min, so a reboot can rewind up to 15 min of it): it adds nothing and
the count continues from the new value — never a negative day.

Live: storage/writer.py calls EnergyToday.add() for each payload with energy_kwh but no energy_today_kwh; the
derived point carries the source reading's ts, so it is deterministic and both stacks write the same rows.
One-time:  python3 server/storage/energy_today.py --backfill [--device D] [--parquet] [--reroll SINCE_TS]
           (upsert; idempotent; --reroll rebuilds the chart rungs from SINCE_TS and bumps the replica epoch)
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


def _archived(parquet_glob: str, device_id: str) -> list[tuple]:
    """(ts, device_type, area, transport, value, schema_v, authoritative) energy_kwh rows from the parquet archive."""
    import glob
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    out = []
    for path in sorted(p for p in glob.glob(parquet_glob) if p.endswith(".parquet")):
        # ParquetFile, not read_table: one file only (see rollup.backfill_from_parquet — hive schema merge)
        t = pq.ParquetFile(path).read(columns=["ts", "device_id", "device_type", "area", "transport", "metric",
                                               "value", "schema_v"])
        t = t.filter(pc.and_(pc.equal(t["device_id"], device_id), pc.equal(t["metric"], "energy_kwh")))
        out += [(r["ts"], r["device_type"], r["area"], r["transport"], r["value"], r["schema_v"], 1)
                for r in t.to_pylist()]
    return out


def backfill(db: Path, control_db: Path, devices: list[str] | None = None,
             parquet_glob: str | None = None) -> int:
    """Walk each lifetime-only meter's whole energy_kwh history in order and (re)write energy_today_kwh into
    hot.db — an UPSERT, so it also corrects rows a live writer derived without the history (its first day).
    With `parquet_glob` the archived history is walked too; its derived rows land in hot.db with their old ts
    and the nightly compactor archives them like any late row. Default devices: those with fewer
    energy_today_kwh rows than energy_kwh rows (an S31 publishes both, so it never qualifies)."""
    conn = sqlite3.connect(str(db))
    tz = house_tz(control_db)
    devs = devices or [d for (d,) in conn.execute(
        "SELECT device_id FROM readings WHERE metric IN ('energy_kwh','energy_today_kwh') GROUP BY device_id "
        "HAVING SUM(metric='energy_today_kwh') < SUM(metric='energy_kwh')")]
    n = 0
    for did in devs:
        blank = sqlite3.connect(":memory:")               # the walk carries its own state from the first row
        blank.execute("CREATE TABLE readings (ts, device_id, metric, value)")
        et = EnergyToday(blank, lambda: tz)
        rows = {r[0]: r for r in (_archived(parquet_glob, did) if parquet_glob else [])}
        rows.update({r[0]: r for r in conn.execute(
            "SELECT ts, device_type, area, transport, value, schema_v, authoritative FROM readings "
            "WHERE device_id=? AND metric='energy_kwh'", (did,))})                # hot wins on overlap
        for ts in sorted(rows):
            _, dtype, area, transport, v, schema_v, auth = rows[ts]
            today = et.add(did, ts, v)
            if today is None:
                continue
            n += conn.execute("INSERT INTO readings (ts, device_id, device_type, area, transport, metric, value, "
                              "unit, schema_v, authoritative) VALUES (?,?,?,?,?,?,?,?,?,?) "
                              "ON CONFLICT(device_id, ts, metric) DO UPDATE SET value=excluded.value "
                              "WHERE readings.value IS NOT excluded.value",
                              (ts, did, dtype, area, transport, "energy_today_kwh", today, "kWh", schema_v,
                               auth)).rowcount
        conn.commit()
        span = f"{min(rows)} .. {max(rows)}" if rows else "none"
        print(f"{did}: {len(rows)} energy_kwh readings walked ({span})")
    conn.close()
    return n


def reroll(db: Path, rung_db: Path, since_ts: str) -> None:
    """Rebuild the chart rungs from `since_ts` (old buckets now hold energy_today_kwh) and bump the replica epoch
    so seeded panels re-seed — rollup.run() only rolls forward. Upserts, so it is safe beside the rollup timer."""
    from server.storage import rollup as R
    raw, rung = sqlite3.connect(str(db)), sqlite3.connect(str(rung_db), timeout=120)   # the rollup timer writes too
    try:
        now = int(time.time())
        until = R.iso_of(now // 60 * 60)
        n = R.rollup_minute(raw, rung, since_ts, until)
        lo = max(R.epoch_of(since_ts) - R.WIDTH_S["1week"], 0)
        out = {"1min": n, **{d: R.cascade(rung, d, s, lo, now + 1) for d, s in R.CASCADE}}
        print(f"rungs rebuilt from {since_ts}: {out}; epoch -> {R.bump_epoch(rung)}")
    finally:
        raw.close()
        rung.close()


def main() -> None:
    sys.path.insert(0, str(REPO))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backfill", action="store_true", required=True)
    ap.add_argument("--db", type=Path, default=REPO / "instance/db/hot.db")
    ap.add_argument("--control-db", type=Path, default=REPO / "instance/db/control.db")
    ap.add_argument("--device", action="append", help="re-derive this device even if it looks complete")
    ap.add_argument("--parquet", nargs="?", const=str(REPO / "instance/db/parquet/year=*/month=*/*.parquet"),
                    help="also walk the archived history (default glob: the parquet archive)")
    ap.add_argument("--reroll", metavar="SINCE_TS", help="then rebuild rungs.db from this ISO ts + bump epoch")
    ap.add_argument("--rung-db", type=Path, default=REPO / "instance/db/rungs.db")
    a = ap.parse_args()
    print(f"wrote {backfill(a.db, a.control_db, a.device, a.parquet)} rows (new or corrected)")
    if a.reroll:
        reroll(a.db, a.rung_db, a.reroll)


if __name__ == "__main__":
    main()
