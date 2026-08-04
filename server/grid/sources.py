"""Shed-signal sources — pluggable (ADR-0037).

Each source answers one question: *is there a curtailment window right now, and when does it end?*
The runner and the decision law do not care where that came from, so swapping the signal is config, not
code — the same shape as `server/weather/sources.py`.

**Why this seam exists at all: PGE publishes no machine-readable load-warning signal.** Peak Time Events
are delivered to enrolled customers by email/SMS only; `portlandgeneral.com/peak-time-events` carries no
event status, no dates, no JSON; and the unofficial `portlandgeneral-api` GraphQL library needs account
credentials and exposes billing/usage, not events. So there is nothing to poll, and the honest design is
to make the trigger a choice rather than pretend one source is authoritative.

Implemented here:
  - `ScheduleSource` — a fixed daily window over a season. No signal, no credentials, no guessing.
  - `NwsAlertSource` — api.weather.gov active alerts for a point (keyless). A heat alert is a *proxy* for
    a peak event, not the event itself: it will fire on days PGE called nothing, and miss any event that
    was not heat-driven.

Declared but NOT implemented, deliberately:
  - `imap` — ingest PGE's own peak-event mail. The highest-fidelity option (it carries the real start and
    stop times), but parsing it means guessing at a message format nobody here has seen. Writing a parser
    against an imagined email would be fabrication; this waits for a real sample.
  - `manual` — a remote "grid event" trigger. Waiting on which path (PWA control vs an ntfy-driven hook).
"""
from __future__ import annotations

import abc
import json
import logging
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .shed import ShedWindow

log = logging.getLogger("ha.grid")

USER_AGENT = "home-automation/1.0 (household grid-shed lane)"


class ShedSource(abc.ABC):
    """A source of curtailment windows. Must be self-contained and must NEVER raise into the runner —
    a source that cannot answer returns None, and the lane simply does not shed (fail toward comfort)."""
    name = "base"

    @abc.abstractmethod
    def window(self, now: float) -> ShedWindow | None:
        ...


# ── helpers ──────────────────────────────────────────────────────────────────────
# The utility's wall clock, NOT the box's. .210 runs on UTC, so reading "17:00-21:00" as box-local would
# have shed 10:00-14:00 Pacific — the wrong four hours, every day, silently. A peak window is defined by
# the utility in its own local time and has to track that timezone's DST, so the zone is explicit.
UTILITY_TZ = "America/Los_Angeles"


def _zone(tz: str | None):
    try:
        return ZoneInfo(tz or UTILITY_TZ)
    except ZoneInfoNotFoundError:                    # no tzdata on the box — refuse rather than guess
        raise SystemExit(f"timezone '{tz or UTILITY_TZ}' unavailable (install tzdata); "
                         "refusing to interpret the peak window in the wrong zone")


def _hhmm(s: str) -> tuple[int, int]:
    h, m = s.strip().split(":")
    return int(h), int(m)


def _local_window(now: float, spec: str, tz: str | None = None) -> tuple[float, float]:
    """'17:00-21:00' -> (start_epoch, end_epoch) for the utility-local day containing `now`."""
    a, b = spec.split("-")
    lt = datetime.fromtimestamp(now, _zone(tz))
    sh, sm = _hhmm(a)
    eh, em = _hhmm(b)
    start = lt.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = lt.replace(hour=eh, minute=em, second=0, microsecond=0)
    if end <= start:                      # window wraps past midnight (e.g. 22:00-02:00)
        end = end + timedelta(days=1)
    return start.timestamp(), end.timestamp()


def _in_season(now: float, season: str | None, tz: str | None = None) -> bool:
    """season = 'MM-DD..MM-DD' in utility-local time, inclusive; may wrap the new year. None = always."""
    if not season:
        return True
    a, b = season.split("..")
    lt = datetime.fromtimestamp(now, _zone(tz))
    md = (lt.month, lt.day)
    sa = tuple(int(x) for x in a.split("-"))
    sb = tuple(int(x) for x in b.split("-"))
    if sa <= sb:
        return sa <= md <= sb
    return md >= sa or md <= sb           # wraps (e.g. 11-01..02-28)


# ── schedule ─────────────────────────────────────────────────────────────────────
@dataclass
class ScheduleSource(ShedSource):
    """A fixed daily window, optionally bounded to a season. PGE's summer peak season is Jun 1 - Sep 30
    with events typically 5-9pm; this shape captures the peak-hour saving whether or not an event is
    actually called."""
    window_spec: str = "17:00-21:00"
    season: str | None = "06-01..09-30"
    tz: str = UTILITY_TZ
    name = "schedule"

    def window(self, now: float) -> ShedWindow | None:
        if not _in_season(now, self.season, self.tz):
            return None
        start, end = _local_window(now, self.window_spec, self.tz)
        if not (start <= now < end):
            return None
        return ShedWindow(start, end, f"scheduled peak window {self.window_spec}", self.name)


# ── NWS active alerts (keyless) ──────────────────────────────────────────────────
@dataclass
class NwsAlertSource(ShedSource):
    """Shed during the daily peak window, but only on days the National Weather Service has an active
    heat alert for this point. A proxy for a PGE peak event, not the event itself — see module docstring.

    api.weather.gov requires a User-Agent and is keyless. Any failure returns None (no shed)."""
    lat: float
    lon: float
    window_spec: str = "17:00-21:00"
    events: tuple = ("Excessive Heat Warning", "Extreme Heat Warning",
                     "Heat Advisory", "Excessive Heat Watch", "Extreme Heat Watch")
    tz: str = UTILITY_TZ
    timeout_s: float = 10.0
    name = "nws"

    def _active_alerts(self) -> list[str]:
        url = f"https://api.weather.gov/alerts/active?point={self.lat:.4f},{self.lon:.4f}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/geo+json"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            data = json.loads(r.read().decode())
        return [f.get("properties", {}).get("event", "") for f in data.get("features", [])]

    def window(self, now: float) -> ShedWindow | None:
        start, end = _local_window(now, self.window_spec, self.tz)
        if not (start <= now < end):
            return None                    # outside the peak window; no need to call out at all
        try:
            alerts = self._active_alerts()
        except Exception as e:             # network/API trouble -> no shed, never an exception upward
            log.warning("NWS alert fetch failed (%s) — not shedding", e)
            return None
        hit = next((a for a in alerts if a in self.events), None)
        if hit is None:
            log.info("no qualifying heat alert (active: %s) — not shedding", alerts or "none")
            return None
        return ShedWindow(start, end, f"{hit} + peak window {self.window_spec}", self.name)


# ── registry ─────────────────────────────────────────────────────────────────────
NOT_IMPLEMENTED = {
    "imap": "ingest PGE's own peak-event mail — highest fidelity, needs a real sample message to parse",
    "manual": "remote 'grid event' trigger — needs a decision on PWA control vs ntfy hook",
}


def build_source(kind: str, *, lat=None, lon=None, window_spec="17:00-21:00",
                 season="06-01..09-30", tz=UTILITY_TZ) -> ShedSource:
    kind = (kind or "").strip().lower()
    if kind == "schedule":
        return ScheduleSource(window_spec=window_spec, season=season, tz=tz)
    if kind == "nws":
        if lat is None or lon is None:
            raise SystemExit("source 'nws' needs HA_GRID_LAT/HA_GRID_LON (or the weather.env pair)")
        return NwsAlertSource(lat=float(lat), lon=float(lon), window_spec=window_spec, tz=tz)
    if kind in NOT_IMPLEMENTED:
        raise SystemExit(f"source '{kind}' is declared but not implemented: {NOT_IMPLEMENTED[kind]}")
    raise SystemExit(f"unknown source '{kind}' (want: schedule | nws | {' | '.join(NOT_IMPLEMENTED)})")
