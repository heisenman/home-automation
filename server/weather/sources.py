"""
Weather sources — pluggable. The internet source can later be replaced by an air-gapped
transfer source (reading data synced during backup) without changing the store or runner.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field

log = logging.getLogger("ha.weather")


@dataclass
class WeatherReading:
    ts: str                  # ISO 8601 UTC, e.g. 2026-06-20T19:00:00Z
    source: str              # e.g. "openmeteo"
    location: str            # label, e.g. "0.0,0.0" or "<YOUR_ZIP> <YOUR_CITY>"
    metrics: dict = field(default_factory=dict)  # {"temperature_c":.., "humidity_pct":.., "pressure_hpa":..}


class WeatherSource(abc.ABC):
    """A source of outdoor weather readings. Implementations must be self-contained so the
    runner/store don't care whether data comes from the internet or an offline transfer."""
    name = "base"

    @abc.abstractmethod
    def fetch(self) -> WeatherReading:
        ...


class OpenMeteoSource(WeatherSource):
    """Open-Meteo current conditions — free, no API key, by latitude/longitude.

    Docs: https://open-meteo.com/en/docs  (temperature_2m °C, relative_humidity_2m %,
    surface_pressure & pressure_msl hPa).
    """
    name = "openmeteo"
    BASE = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, lat: float, lon: float, location_label: str | None = None):
        self.lat = lat
        self.lon = lon
        self.location = location_label or f"{lat:.4f},{lon:.4f}"

    def fetch(self) -> WeatherReading:
        import httpx
        params = {
            "latitude": self.lat,
            "longitude": self.lon,
            "current": "temperature_2m,relative_humidity_2m,surface_pressure,pressure_msl",
            "timezone": "UTC",
        }
        with httpx.Client(timeout=15) as c:
            r = c.get(self.BASE, params=params)
            r.raise_for_status()
            j = r.json()
        cur = j.get("current", {})
        ts = cur.get("time")
        if ts and len(ts) == 16:   # 'YYYY-MM-DDTHH:MM' (UTC) -> normalize
            ts = ts + ":00Z"
        m: dict = {}
        if cur.get("temperature_2m") is not None:
            m["temperature_c"] = round(float(cur["temperature_2m"]), 1)
        if cur.get("relative_humidity_2m") is not None:
            m["humidity_pct"] = int(round(float(cur["relative_humidity_2m"])))
        if cur.get("surface_pressure") is not None:        # actual local pressure
            m["pressure_hpa"] = round(float(cur["surface_pressure"]), 1)
        if cur.get("pressure_msl") is not None:            # sea-level-normalized
            m["pressure_msl_hpa"] = round(float(cur["pressure_msl"]), 1)
        return WeatherReading(ts=ts, source=self.name, location=self.location, metrics=m)


_HOURLY_VARS = "temperature_2m,relative_humidity_2m,surface_pressure,pressure_msl"


def _hourly_to_readings(j: dict, source: str, location: str) -> list[WeatherReading]:
    """Convert an Open-Meteo hourly response (parallel arrays) into per-hour readings."""
    h = j.get("hourly", {})
    times = h.get("time", []) or []
    out: list[WeatherReading] = []
    for i, t in enumerate(times):
        ts = t + ":00Z" if len(t) == 16 else t
        m: dict = {}
        def put(key, arr, cast, unit_round=1):
            v = (h.get(arr) or [None] * len(times))[i]
            if v is not None:
                m[key] = cast(v)
        put("temperature_c", "temperature_2m", lambda v: round(float(v), 1))
        put("humidity_pct", "relative_humidity_2m", lambda v: int(round(float(v))))
        put("pressure_hpa", "surface_pressure", lambda v: round(float(v), 1))
        put("pressure_msl_hpa", "pressure_msl", lambda v: round(float(v), 1))
        if m:
            out.append(WeatherReading(ts=ts, source=source, location=location, metrics=m))
    return out


def fetch_archive(lat: float, lon: float, start_date: str, end_date: str,
                  location_label: str | None = None) -> list[WeatherReading]:
    """Historical hourly weather from Open-Meteo's archive (ERA5; free, no key). Dates YYYY-MM-DD."""
    import httpx
    loc = location_label or f"{lat:.4f},{lon:.4f}"
    params = {"latitude": lat, "longitude": lon, "start_date": start_date, "end_date": end_date,
              "hourly": _HOURLY_VARS, "timezone": "UTC"}
    with httpx.Client(timeout=60) as c:
        r = c.get("https://archive-api.open-meteo.com/v1/archive", params=params)
        r.raise_for_status()
        return _hourly_to_readings(r.json(), "openmeteo", loc)


def fetch_recent(lat: float, lon: float, past_days: int = 16,
                 location_label: str | None = None) -> list[WeatherReading]:
    """Recent hourly weather (past_days up to 92) from the forecast API — fills the gap between
    the archive's ~5-day lag and now."""
    import httpx
    loc = location_label or f"{lat:.4f},{lon:.4f}"
    params = {"latitude": lat, "longitude": lon, "hourly": _HOURLY_VARS,
              "past_days": min(max(past_days, 1), 92), "forecast_days": 1, "timezone": "UTC"}
    with httpx.Client(timeout=60) as c:
        r = c.get("https://api.open-meteo.com/v1/forecast", params=params)
        r.raise_for_status()
        return _hourly_to_readings(r.json(), "openmeteo", loc)


def geocode_zip(zip_code: str, country: str = "us") -> tuple[float, float, str]:
    """Resolve a postal code to (lat, lon, label) via zippopotam.us (free, no key).
    Prefer passing lat/lon directly for 'better geolocation'; this is the zip convenience path."""
    import httpx
    with httpx.Client(timeout=15) as c:
        r = c.get(f"https://api.zippopotam.us/{country}/{zip_code}")
        r.raise_for_status()
        j = r.json()
    place = j["places"][0]
    lat = float(place["latitude"])
    lon = float(place["longitude"])
    label = f'{zip_code} {place.get("place name", "")}'.strip()
    return lat, lon, label
