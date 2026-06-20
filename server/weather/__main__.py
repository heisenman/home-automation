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

from .sources import OpenMeteoSource, geocode_zip
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
    ap.add_argument("--log-level", default="INFO")
    a = ap.parse_args()

    logging.basicConfig(level=getattr(logging, a.log_level),
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
                        stream=sys.stdout)

    source = build_source(a)
    store = WeatherStore(a.db, a.table)
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
