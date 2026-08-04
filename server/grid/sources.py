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


def _iso(s: str | None) -> float | None:
    """NWS ISO-8601 with offset (e.g. '2026-08-04T12:00:00-07:00') -> epoch. None/unparseable -> None,
    which callers read as 'unbounded on that side'."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        log.warning("unparseable NWS timestamp %r", s)
        return None


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

    def _alerts(self) -> list[tuple[str, float | None, float | None]]:
        """[(event, onset_epoch, ends_epoch)] for the point. `/alerts/active` includes alerts that are
        ISSUED BUT NOT YET IN EFFECT, so the times are not optional detail — see `window`."""
        url = f"https://api.weather.gov/alerts/active?point={self.lat:.4f},{self.lon:.4f}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/geo+json"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            data = json.loads(r.read().decode())
        out = []
        for f in data.get("features", []):
            p = f.get("properties", {}) or {}
            out.append((p.get("event", ""), _iso(p.get("onset") or p.get("effective")),
                        _iso(p.get("ends") or p.get("expires"))))
        return out

    def window(self, now: float) -> ShedWindow | None:
        start, end = _local_window(now, self.window_spec, self.tz)
        if not (start <= now < end):
            return None                    # outside the peak window; no need to call out at all
        try:
            alerts = self._alerts()
        except Exception as e:             # network/API trouble -> no shed, never an exception upward
            log.warning("NWS alert fetch failed (%s) — not shedding", e)
            return None

        # An alert must be IN EFFECT NOW, not merely published. `/alerts/active` lists alerts the moment
        # they are issued: on 2026-08-03 at 20:30 PDT the feed already carried a Heat Advisory whose onset
        # was 2026-08-04 12:00 — shedding on that would have curtailed the house for a heat wave that had
        # not started. A missing onset means "already in effect"; a missing end means open-ended.
        live = [(e, o, x) for (e, o, x) in alerts
                if e in self.events and (o is None or o <= now) and (x is None or now < x)]
        if not live:
            # in the UTILITY's zone, not the box's — .210 is UTC, and "Heat Advisory@19:00" for a noon
            # PDT onset is exactly the sort of log line that sends someone chasing a phantom bug.
            z = _zone(self.tz)
            pending = [f"{e}@{datetime.fromtimestamp(o, z).strftime('%m-%d %H:%M %Z')}"
                       for (e, o, x) in alerts if e in self.events and o is not None and o > now]
            log.info("no qualifying heat alert in effect (%s) — not shedding",
                     f"not yet: {', '.join(pending)}" if pending else
                     f"active: {[a for a, _, _ in alerts] or 'none'}")
            return None

        # Clip the peak window to the alert's own end, so a shed cannot outlive the condition that
        # justified it (an advisory ending at 18:00 must not curtail through 21:00).
        event, _, ends = min(live, key=lambda t: (t[2] is None, t[2]))
        if ends is not None and ends < end:
            end = ends
        if end <= now:
            return None
        return ShedWindow(start, end, f"{event} + peak window {self.window_spec}", self.name)


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
