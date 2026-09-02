#!/usr/bin/env python3
"""
Power regression watch — notice when a metered box starts drawing more than its characterized baseline
(ADR-0039).

WHY THIS EXISTS. The 2026-07-04 campaign (docs/reviews/2026-07-04-power-optimization-day9.md) characterized
the platform and concluded: idle is at the floor, no software lever left. What it did NOT leave behind was
anything that WATCHES. "Is power still okay?" has been answered by a human glancing at a wall panel — which
is how 2026-09-02 went: Hugh noticed a display and asked. That's the operator dead-end this project treats
as a structural bug ([[if-it-needs-me-structure-is-wrong]]).

TWO THINGS IT CATCHES, and the second is the one that bites:

  1. DRIFT  — the rolling-window mean sits above `baseline_w + tolerance_w` for `consecutive` checks.
              Window-mean plus consecutive-check hysteresis, so a compile or a backup can't trip it.
  2. STALE  — a meter that has stopped reporting. This is the ADR-0032 failure mode: `airgap_router_pm`
              and `failover_pm` were migrated .210 -> ha-2, landed unregistered, and had their telemetry
              silently DROPPED for ~23h. A watcher that only ever compares numbers it receives will call
              a dead meter "fine" forever, so absence has to be its own alarm.

BASELINES ARE COMMITTED, NOT LEARNED (provisioning/power-baselines.yaml). An auto-updating baseline
absorbs exactly the slow regression it is supposed to catch — it would have ratified any creep as the new
normal. Re-characterizing is a deliberate act: `--characterize` prints a YAML block from real measured
history for a human to read and commit.

SOURCE-AGNOSTIC by design: reads the local sqlite hot.db when the meters live there (ha-2, the record of
record) and falls back to ha-2's HTTP API when they don't (.210, where this currently runs). Same code
either way, so moving it to ha-2 later is a systemd change and not a rewrite.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]

CONFIG = Path(os.environ.get("HA_POWER_BASELINES", str(REPO / "provisioning" / "power-baselines.yaml")))
STATE_FILE = REPO / "instance" / ".power-watch-state.json"
DB = Path(os.environ.get("HA_DB", str(REPO / "instance" / "db" / "hot.db")))
API = os.environ.get("HA_POWER_API", "http://192.168.1.210:8123")   # ha-2 on the air-gap side
BROKER = os.environ.get("HA_POWER_BROKER", "127.0.0.1")
BROKER_PORT = int(os.environ.get("HA_POWER_BROKER_PORT", "1883"))
METRIC = "power_w"
HTTP_TIMEOUT = float(os.environ.get("HA_POWER_HTTP_TIMEOUT", "20"))


def _log(msg: str) -> None:
    print(f"ha.power_watch — {msg}", flush=True)


# ── pure evaluation core (no I/O — this is what the tests drive) ────────────────────────────────────────
@dataclass
class Meter:
    device_id: str
    baseline_w: float
    tolerance_w: float
    label: str = ""
    window_h: float = 6.0
    stale_after_h: float = 2.0
    consecutive: int = 2

    @property
    def ceiling_w(self) -> float:
        return self.baseline_w + self.tolerance_w


@dataclass
class Verdict:
    device_id: str
    status: str                                  # "ok" | "high" | "stale"
    mean_w: float | None = None
    n: int = 0
    alerts: list[dict] = field(default_factory=list)
    note: str = ""


def window_means(samples: list[tuple[float, float]], window_h: float) -> list[float]:
    """Split samples into consecutive non-overlapping window-sized buckets and return each bucket's mean.

    This is the distribution the alert actually tests against, so it's the one a baseline must be measured
    from. Partial trailing buckets are dropped — a half-full window's mean isn't comparable to a full one.
    """
    if not samples or window_h <= 0:
        return []
    ordered = sorted(samples)
    span = window_h * 3600
    t0 = ordered[0][0]
    buckets: dict[int, list[float]] = {}
    for ts, v in ordered:
        buckets.setdefault(int((ts - t0) // span), []).append(v)
    last = int((ordered[-1][0] - t0) // span)
    # the final bucket is only whole if the data actually reaches its end
    complete = [i for i in sorted(buckets) if i < last]
    return [statistics.fmean(buckets[i]) for i in complete]


def _alert(kind: str, severity: str, device_id: str, message: str, **extra) -> dict:
    # Stable id per (kind, device) so a re-fire dedups rather than stacking in the PWA banner.
    return {"id": f"{kind}:{device_id}", "kind": kind, "severity": severity, "device_id": device_id,
            "message": message, **extra}


def evaluate(m: Meter, samples: list[tuple[float, float]], state: dict, now: float) -> Verdict:
    """Pure: (meter config, [(epoch_ts, watts)], prior state, now) -> verdict. MUTATES `state`.

    `samples` is everything inside the window; the caller does the fetching. Edge-triggered alerts.
    """
    window_start = now - m.window_h * 3600
    inside = [v for ts, v in samples if ts >= window_start]

    # ── absence is its own alarm (ADR-0032) ──────────────────────────────────────────────────────────
    if not inside:
        newest = max((ts for ts, _ in samples), default=None)
        age_h = (now - newest) / 3600 if newest else None
        if age_h is None or age_h >= m.stale_after_h:
            v = Verdict(m.device_id, "stale", note="no readings in window")
            if not state.get("stale_alerted"):
                state["stale_alerted"] = True
                age_txt = f"{age_h:.1f}h" if age_h is not None else "ever"
                v.alerts.append(_alert("power_meter_stale", "warning", m.device_id,
                                       f"power meter '{m.device_id}' has reported nothing for {age_txt} "
                                       f"— a silently dropped meter reads as healthy forever (ADR-0032)",
                                       age_h=round(age_h, 2) if age_h is not None else None))
            return v
        return Verdict(m.device_id, "ok", note=f"thin window, newest {age_h:.1f}h old")

    if state.get("stale_alerted"):
        state["stale_alerted"] = False
        # recovery is worth saying out loud — it closes the banner the stale alert opened
        recovered = _alert("power_meter_ok", "info", m.device_id,
                           f"power meter '{m.device_id}' is reporting again ({len(inside)} readings)")
    else:
        recovered = None

    mean_w = statistics.fmean(inside)
    v = Verdict(m.device_id, "ok", mean_w=mean_w, n=len(inside))
    if recovered:
        v.alerts.append(recovered)

    # ── drift ────────────────────────────────────────────────────────────────────────────────────────
    if mean_w > m.ceiling_w:
        state["breaches"] = state.get("breaches", 0) + 1
        n = state["breaches"]
        if n >= m.consecutive:
            v.status = "high"
            if not state.get("high_alerted"):
                state["high_alerted"] = True
                over = mean_w - m.baseline_w
                v.alerts.append(_alert("power_regression", "warning", m.device_id,
                                       f"{m.label or m.device_id}: {m.window_h:.0f}h mean {mean_w:.2f} W "
                                       f"is {over:+.2f} W over baseline {m.baseline_w:.2f} W "
                                       f"(ceiling {m.ceiling_w:.2f} W) for {n} consecutive checks",
                                       mean_w=round(mean_w, 2), baseline_w=m.baseline_w,
                                       ceiling_w=round(m.ceiling_w, 2), n=len(inside)))
        else:
            v.note = f"over ceiling {n}/{m.consecutive} — within hysteresis"
        return v

    state["breaches"] = 0
    if state.get("high_alerted"):
        state["high_alerted"] = False
        v.alerts.append(_alert("power_normal", "info", m.device_id,
                               f"{m.label or m.device_id}: back to {mean_w:.2f} W, under the "
                               f"{m.ceiling_w:.2f} W ceiling",
                               mean_w=round(mean_w, 2)))
    return v


# ── config ─────────────────────────────────────────────────────────────────────────────────────────────
def load_meters(path: Path = CONFIG) -> list[Meter]:
    raw = yaml.safe_load(path.read_text()) or {}
    defaults = raw.get("defaults", {}) or {}
    out = []
    for entry in raw.get("meters", []) or []:
        cfg = {**defaults, **entry}
        out.append(Meter(device_id=cfg["device_id"], baseline_w=float(cfg["baseline_w"]),
                         tolerance_w=float(cfg["tolerance_w"]), label=cfg.get("label", ""),
                         window_h=float(cfg.get("window_h", 6)),
                         stale_after_h=float(cfg.get("stale_after_h", 2)),
                         consecutive=int(cfg.get("consecutive", 2))))
    return out


# ── readers: local sqlite where the meters live, else ha-2's API ────────────────────────────────────────
def _parse_ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def read_sqlite(device_id: str, since: float) -> list[tuple[float, float]]:
    if not DB.exists():
        return []
    iso = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        # read-only: this runs alongside the writer and must never take a write lock
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        rows = con.execute("SELECT ts, value FROM readings WHERE device_id=? AND metric=? AND ts>=?",
                           (device_id, METRIC, iso)).fetchall()
        con.close()
    except sqlite3.Error as exc:
        _log(f"sqlite read failed for {device_id}: {exc}")
        return []
    out = []
    for ts, val in rows:
        try:
            out.append((_parse_ts(ts), float(val)))
        except (ValueError, TypeError):
            continue
    return out


API_ROW_LIMIT = 10000
# Meters report every ~30s => ~2880 rows/day. Page in slices that stay well under the API's row cap:
# the endpoint TRUNCATES silently (`truncated: true` in the body, easy to never look at), and a truncated
# read would quietly characterize a baseline from partial history.
API_SLICE_S = float(os.environ.get("HA_POWER_API_SLICE_S", str(2 * 86400)))


def _api_slice(device_id: str, start: float, end: float) -> tuple[list[tuple[float, float]], bool]:
    s = datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    e = datetime.fromtimestamp(end, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # device_id comes from a committed config, but it still lands in a URL PATH — quote it with an empty
    # safe set so a stray '/' or '..' can't walk to a different endpoint. And pin the scheme: urlopen()
    # honours file://, so an unvalidated HA_POWER_API would turn this into a local-file reader.
    if not API.startswith(("http://", "https://")):
        _log(f"refusing HA_POWER_API={API!r}: only http/https")
        return [], False
    url = (f"{API}/devices/{urllib.parse.quote(device_id, safe='')}/readings?metric={METRIC}"
           f"&start={s}&end={e}&limit={API_ROW_LIMIT}")
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as r:
            payload = json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # The air-gap link being down is the linkwatch's problem, not ours — degrade to "no data" and let
        # the stale check speak, rather than inventing a power verdict from nothing.
        _log(f"api read failed for {device_id}: {exc}")
        return [], False
    out = []
    for row in payload.get("readings", []):
        try:
            out.append((_parse_ts(row["ts"]), float(row["value"])))
        except (KeyError, ValueError, TypeError):
            continue
    return out, bool(payload.get("truncated"))


def read_api(device_id: str, since: float, until: float | None = None) -> list[tuple[float, float]]:
    until = time.time() if until is None else until
    out: list[tuple[float, float]] = []
    start = since
    while start < until:
        end = min(start + API_SLICE_S, until)
        rows, truncated = _api_slice(device_id, start, end)
        if truncated:
            # Loud, not silent: a truncated slice means the baseline would be computed from partial data.
            _log(f"WARNING {device_id}: API truncated a slice at {API_ROW_LIMIT} rows "
                 f"({datetime.fromtimestamp(start, timezone.utc):%Y-%m-%dT%H:%MZ}..) — "
                 f"lower HA_POWER_API_SLICE_S")
        out.extend(rows)
        start = end
    return out


# How far short of `since` a local read may fall and still count as covering the window. One rollup-ish
# slack period, not a free pass.
COVERAGE_SLACK_S = float(os.environ.get("HA_POWER_COVERAGE_SLACK_S", "900"))


def read_samples(device_id: str, since: float) -> tuple[list[tuple[float, float]], str]:
    """Local DB when it actually COVERS the window, else the API. Returns (samples, source).

    "Has rows" is not the same as "has the window". `hot.db` is pruned daily by the compactor, so on ha-2 it
    holds roughly today-so-far — a few hours after midnight, and less right after a compaction. Preferring
    it on mere non-emptiness would silently evaluate a 6h window against 2h of data, and would let
    `--characterize` print a confident 7-day baseline computed from an afternoon. Same silent-partial-data
    failure as the API's 10k-row truncation, so it gets the same treatment: detect it, say so, use the
    source that actually has the range.
    """
    rows = read_sqlite(device_id, since)
    if rows and min(ts for ts, _ in rows) <= since + COVERAGE_SLACK_S:
        return rows, "sqlite"
    if rows:
        oldest = min(ts for ts, _ in rows)
        short_h = (oldest - since) / 3600
        api_rows = read_api(device_id, since)
        if api_rows:
            return api_rows, "api(local-db short by %.1fh)" % short_h
        # API unavailable too — use what we have, but never let the shortfall pass unremarked.
        _log(f"WARNING {device_id}: local DB covers only from {short_h:.1f}h after the requested start and "
             f"the API is unreachable — evaluating on PARTIAL history")
        return rows, "sqlite(partial)"
    return read_api(device_id, since), "api"


# ── effects ────────────────────────────────────────────────────────────────────────────────────────────
def _mqtt():
    sys.path.insert(0, str(REPO))
    import paho.mqtt.client as mqtt
    from server.util.mqtt_creds import apply_credentials
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    apply_credentials(c)
    c.connect(BROKER, BROKER_PORT, keepalive=10)
    c.loop_start()
    return c


def publish(alerts: list[dict], verdicts: list[Verdict], dry: bool) -> None:
    beacon = {"ts": int(time.time()),
              "meters": [{"device_id": v.device_id, "status": v.status,
                          "mean_w": round(v.mean_w, 2) if v.mean_w is not None else None, "n": v.n}
                         for v in verdicts]}
    if dry:
        for a in alerts:
            _log(f"[dry-run] would alert: {a['kind']} — {a['message']}")
        _log(f"[dry-run] would beacon: {json.dumps(beacon)}")
        return
    try:
        c = _mqtt()
        for a in alerts:
            a.setdefault("ts", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            c.publish("home/_alert/new", json.dumps(a), qos=1)
            _log(f"{a['kind'].upper()}: {a['message']}")
        c.publish("home/_power/watch/status", json.dumps(beacon), qos=1, retain=True)
        c.loop_stop()
        c.disconnect()
    except Exception as exc:
        _log(f"publish failed (bus down?): {exc}")


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def check_once(dry: bool) -> int:
    meters = load_meters()
    if not meters:
        _log(f"no meters configured in {CONFIG} — nothing to watch")
        return 0
    state = {} if dry else _load_state()
    now = time.time()
    alerts: list[dict] = []
    verdicts: list[Verdict] = []

    for m in meters:
        # Pull a bit beyond the window so a stale meter's true age is knowable, not just "nothing here".
        lookback = max(m.window_h, m.stale_after_h) * 3600 * 2
        samples, source = read_samples(m.device_id, now - lookback)
        st = state.setdefault(m.device_id, {})
        v = evaluate(m, samples, st, now)
        verdicts.append(v)
        alerts.extend(v.alerts)
        mean_txt = f"{v.mean_w:.2f}W" if v.mean_w is not None else "--"
        _log(f"{m.device_id}: {v.status} mean={mean_txt} n={v.n} "
             f"baseline={m.baseline_w:.2f} ceiling={m.ceiling_w:.2f} src={source}"
             + (f" | {v.note}" if v.note else ""))

    publish(alerts, verdicts, dry)
    if not dry:
        _save_state(state)
    return 1 if any(v.status != "ok" for v in verdicts) else 0


def characterize(days: float) -> int:
    """Print a baselines block measured from real history. DELIBERATE re-characterization — the operator
    reads it and commits it. Never written automatically: a self-updating baseline would ratify the very
    drift this tool exists to catch."""
    now = time.time()
    since = now - days * 86400
    try:
        existing = {m.device_id: m for m in load_meters()}
    except (FileNotFoundError, KeyError):
        existing = {}
    ids = list(existing) or [d.strip() for d in os.environ.get("HA_POWER_METERS", "").split(",") if d.strip()]
    if not ids:
        _log("no meters to characterize (empty config and HA_POWER_METERS unset)")
        return 1
    print(f"# measured over {days:g} day(s) ending {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}")
    print("meters:")
    for did in ids:
        samples, source = read_samples(did, since)
        vals = [v for _, v in samples]
        if not vals:
            print(f"  # {did}: NO DATA in the window (src={source}) — left unchanged")
            continue
        window_h = existing[did].window_h if did in existing else 6.0
        # CHARACTERIZE THE STATISTIC WE ACTUALLY ALERT ON. The check compares a window MEAN, which is far
        # smoother than the raw samples — deriving tolerance from instantaneous spread (p95 of individual
        # readings) produced a ceiling roughly 2x baseline on failover_pm, i.e. a detector that could never
        # fire. So bucket the history into window-sized blocks and measure the spread of the BLOCK MEANS.
        means = window_means(samples, window_h)
        if len(means) < 2:
            print(f"  # {did}: only {len(means)} full {window_h:g}h window(s) in {days:g}d "
                  f"— widen --characterize before trusting this")
        base = statistics.fmean(means) if means else statistics.fmean(vals)
        worst = max(means) if means else base
        # Cover the worst window actually observed, plus a small margin, floored so a very quiet meter
        # doesn't get a hair-trigger. Interpretable: "no window in the measured history would have alerted."
        tol = max(1.0, round(worst - base + 0.5, 1))
        label = existing[did].label if did in existing else ""
        print(f"  - device_id: {did}")
        if label:
            print(f"    label: {label!r}")
        print(f"    baseline_w: {base:.2f}")
        print(f"    tolerance_w: {tol:.1f}")
        print(f"    # {len(means)}x{window_h:g}h windows: mean={base:.2f} worst={worst:.2f} "
              f"best={min(means) if means else base:.2f} | raw n={len(vals)} "
              f"min={min(vals):.1f} max={max(vals):.1f} src={source}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Watch metered power for drift above a characterized baseline")
    p.add_argument("--dry-run", action="store_true", help="evaluate + log, publish nothing, keep no state")
    p.add_argument("--characterize", type=float, metavar="DAYS",
                   help="print a measured baselines block for DAYS of history (for a human to commit)")
    p.add_argument("--status", action="store_true", help="print the persisted state and exit")
    args = p.parse_args()
    if args.status:
        print(json.dumps(_load_state(), indent=2))
        return 0
    if args.characterize:
        return characterize(args.characterize)
    return check_once(args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
