"""
Standalone weather recorder. Polls a WeatherSource and records to the weather store.

  # one-shot (what the systemd timer runs):
  python3 -m server.weather --once --lat 0.0 --lon 0.0
  # or by zip:
  python3 -m server.weather --once --zip <YOUR_ZIP>
  # continuous (poll every 15 min):
  python3 -m server.weather --lat 0.0 --lon 0.0 --interval 900

Config can also come from env: HA_WEATHER_LAT / HA_WEATHER_LON / HA_WEATHER_ZIP /
HA_WEATHER_DB / HA_WEATHER_TABLE / HA_WEATHER_INTERVAL.

Air-gap path: replace `build_source()`'s OpenMeteoSource with a transfer-reading source
(data synced during backup); the store and loop stay identical.
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from .sources import OpenMeteoSource, geocode_zip, fetch_archive, fetch_recent
from .store import WeatherStore

log = logging.getLogger("ha.weather")


def _envf(key: str):
    v = os.environ.get(key)
    return float(v) if v else None


def build_source(args):
    if args.lat is not None and args.lon is not None:
        return OpenMeteoSource(args.lat, args.lon, args.label)
    if args.zip:
        lat, lon, label = geocode_zip(args.zip, args.country)
        log.info("geocoded %s -> %.4f,%.4f (%s)", args.zip, lat, lon, label)
        return OpenMeteoSource(lat, lon, args.label or label)
    raise SystemExit("location required: pass --lat/--lon (preferred) or --zip")


def main() -> None:
    ap = argparse.ArgumentParser(description="Standalone internet weather recorder")
    ap.add_argument("--lat", type=float, default=_envf("HA_WEATHER_LAT"))
    ap.add_argument("--lon", type=float, default=_envf("HA_WEATHER_LON"))
    ap.add_argument("--zip", default=os.environ.get("HA_WEATHER_ZIP"))
    ap.add_argument("--country", default=os.environ.get("HA_WEATHER_COUNTRY", "us"))
    ap.add_argument("--label", default=os.environ.get("HA_WEATHER_LABEL"))
    ap.add_argument("--db", type=Path,
                    default=os.environ.get("HA_WEATHER_DB", "instance/db/weather.db"))
    ap.add_argument("--table", default=os.environ.get("HA_WEATHER_TABLE", "weather"))
    ap.add_argument("--interval", type=int,
                    default=int(os.environ.get("HA_WEATHER_INTERVAL", "900")))
    ap.add_argument("--once", action="store_true", help="fetch once and exit (for a timer)")
    ap.add_argument("--backfill", action="store_true",
                    help="historical backfill (archive + recent) instead of live poll")
    ap.add_argument("--start", help="backfill start date YYYY-MM-DD")
    ap.add_argument("--end", help="backfill end date YYYY-MM-DD (default: today)")
    ap.add_argument("--log-level", default="INFO")
    a = ap.parse_args()

    logging.basicConfig(level=getattr(logging, a.log_level),
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
                        stream=sys.stdout)

    source = build_source(a)   # resolves lat/lon/label (also for backfill)
    store = WeatherStore(a.db, a.table)

    if a.backfill:
        import datetime
        end = a.end or datetime.date.today().isoformat()
        lat, lon, label = source.lat, source.lon, source.location
        readings = []
        if a.start:
            log.info("archive %s..%s @ %s", a.start, end, label)
            readings += fetch_archive(lat, lon, a.start, end, label)
        log.info("recent (past 92d) @ %s", label)
        readings += fetch_recent(lat, lon, 92, label)
        new = 0
        for rd in readings:
            new += store.store(rd)
        log.info("backfill done: %d hourly readings fetched, %d new rows", len(readings), new)
        return
    log.info("weather source=%s location=%s -> %s/%s interval=%ds",
             source.name, source.location, a.db, a.table, a.interval)

    def tick() -> None:
        try:
            reading = source.fetch()
            n = store.store(reading)
            log.info("%s  %s  %s  (+%d rows)", reading.ts, source.location, reading.metrics, n)
        except Exception as exc:
            log.error("fetch/store failed: %s", exc)

    tick()
    if a.once:
        return
    while True:
        time.sleep(a.interval)
        tick()


if __name__ == "__main__":
    main()
