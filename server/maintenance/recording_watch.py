#!/usr/bin/env python3
"""
Recording watch — is every device actually still recording? (ADR-0040)

THE FAILURE THIS EXISTS FOR (2026-08-31 → 2026-09-02). A brownout cold-booted the edge fleet. Two nodes
never rejoined the (then-hidden) SSID, so `gas_hbed` and `gas_kitchen` went dark. They stayed dark for
**two days** and nothing anywhere said so. Hugh found it by looking at the PWA map.

`ha-gap-watcher.timer` runs daily and exists to find per-device data gaps. It reported
"0 device(s) with gaps to backfill" on 08-30, 08-31, 09-01 and 09-02 — four clean runs straight through the
outage. It is not broken; it is structurally incapable of seeing this:

    def find_gaps(times_sorted, min_gap_s):
        return [(a, b, b - a) for a, b in zip(times_sorted, times_sorted[1:]) if b - a > min_gap_s]

It pairs CONSECUTIVE READINGS. A device that stops reporting produces no later reading, so the trailing
silence yields no pair and therefore no gap. Interior gaps are found; a device that flatlines and stays
dead is invisible. Once the death passes the lookback window the row set is empty and it is skipped
entirely — doubly silent. (It is also a BACKFILL router, not a liveness monitor; recovering recoverable
history is a different job from noticing loss.)

So this tool inverts the question. It does not ask "are the readings I received well spaced?" — it asks
"**is everything that should be reporting, reporting?**", driven by the ROSTER, not by the data. Absence is
the alarm. That is the same lesson as `power_watch`'s stale-meter check and the ADR-0032 silent drop, and
it keeps being learned the expensive way.

Source is `device_last_seen` (device_id → last_ts), which is exactly right for this: it is keyed by device
and it **retains a dead device's last timestamp** rather than losing it with the pruned readings. Verified:
it still held `gas_kitchen` at 62.1h stale and `e1001_c_office` at 885h while both were absent everywhere
else.

Two outputs, because Hugh asked for both alerting and reassurance:
  * ALERTS  — edge-triggered `device_not_recording` / `device_recording_ok`, on every tick (~30 min).
  * DIGEST  — a twice-daily roster summary published even when everything is fine, so "the system is OK"
              is a signal that actually arrives instead of being inferred from silence. Silence is exactly
              what failed here; a health check that only speaks up on failure is indistinguishable from a
              health check that has itself died.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]

CONFIG = Path(os.environ.get("HA_RECORDING_EXPECTATIONS",
                             str(REPO / "provisioning" / "recording-expectations.yaml")))
REGISTRY = Path(os.environ.get("HA_REGISTRY", str(REPO / "instance" / "devices.yaml")))
STATE_FILE = Path(os.environ.get("HA_RECORDING_STATE", str(REPO / "instance" / ".recording-watch-state.json")))
DB = Path(os.environ.get("HA_DB", str(REPO / "instance" / "db" / "hot.db")))
BROKER = os.environ.get("HA_RECORDING_BROKER", "127.0.0.1")
BROKER_PORT = int(os.environ.get("HA_RECORDING_BROKER_PORT", "1883"))

OK, LATE, DORMANT, NEVER = "ok", "late", "dormant", "never"


def _log(msg: str) -> None:
    print(f"ha.recording_watch — {msg}", flush=True)


# ── pure core ───────────────────────────────────────────────────────────────────────────────────────────
@dataclass
class Expectations:
    default_stale_s: float = 3600.0          # a device quiet this long is LATE -> alert
    dormant_after_s: float = 7 * 86400.0     # quiet this long = long-retired; digest only, never alert
    per_device: dict = field(default_factory=dict)     # device_id -> stale_s
    per_type: dict = field(default_factory=dict)       # device_type -> stale_s
    ignore: set = field(default_factory=set)           # device_ids never reported on at all

    def stale_s(self, device_id: str, device_type: str | None) -> float:
        if device_id in self.per_device:
            return float(self.per_device[device_id])
        if device_type and device_type in self.per_type:
            return float(self.per_type[device_type])
        return self.default_stale_s


@dataclass
class Row:
    device_id: str
    device_type: str | None
    area: str | None
    last_ts: float | None                    # epoch, or None = never reported


@dataclass
class Status:
    device_id: str
    state: str
    age_s: float | None
    threshold_s: float
    area: str | None = None


def classify(rows: list[Row], exp: Expectations, now: float) -> list[Status]:
    """Roster in, per-device verdict out. Pure."""
    out = []
    for r in sorted(rows, key=lambda x: x.device_id):
        if r.device_id in exp.ignore:
            continue
        thr = exp.stale_s(r.device_id, r.device_type)
        if r.last_ts is None:
            out.append(Status(r.device_id, NEVER, None, thr, r.area))
            continue
        age = now - r.last_ts
        if age <= thr:
            state = OK
        elif age >= exp.dormant_after_s:
            # Long gone. Alerting on these on day one would bury the signal we actually care about under
            # a pile of retired hardware — they belong in the digest, where a human can retire them.
            state = DORMANT
        else:
            state = LATE
        out.append(Status(r.device_id, state, age, thr, r.area))
    return out


def _alert(kind: str, severity: str, device_id: str, message: str, **extra) -> dict:
    return {"id": f"{kind}:{device_id}", "kind": kind, "severity": severity, "device_id": device_id,
            "message": message, **extra}


def diff_alerts(statuses: list[Status], state: dict) -> list[dict]:
    """Edge-triggered: alert on the transition into LATE, and once on recovery. MUTATES `state`."""
    alerts = []
    for s in statuses:
        was = state.get(s.device_id, {})
        if s.state == LATE:
            if not was.get("alerted"):
                state.setdefault(s.device_id, {})["alerted"] = True
                hrs = (s.age_s or 0) / 3600
                alerts.append(_alert(
                    "device_not_recording", "critical", s.device_id,
                    f"{s.device_id}"
                    + (f" ({s.area})" if s.area else "")
                    + f" has not recorded for {hrs:.1f}h "
                      f"(threshold {s.threshold_s / 3600:.1f}h) — it is registered and was reporting, "
                      f"so this is loss, not absence of hardware",
                    age_h=round(hrs, 2), threshold_h=round(s.threshold_s / 3600, 2), area=s.area))
        elif s.state == OK and was.get("alerted"):
            state[s.device_id]["alerted"] = False
            alerts.append(_alert("device_recording_ok", "info", s.device_id,
                                 f"{s.device_id} is recording again"))
    # a device that vanished from the roster entirely shouldn't keep a latch forever
    live = {s.device_id for s in statuses}
    for did in [d for d in state if d not in live]:
        state.pop(did, None)
    return alerts


def digest_due(now: float, hours: list[int], state: dict) -> bool:
    """True at most once per configured hour-slot per day. Slot identity is (date, hour) so a missed tick
    still fires late rather than skipping the day — the digest is the 'still alive' signal, so dropping one
    silently would defeat its whole purpose."""
    t = datetime.fromtimestamp(now, timezone.utc)
    slot = max((h for h in sorted(hours) if h <= t.hour), default=None)
    if slot is None:
        return False
    key = f"{t.date().isoformat()}:{slot:02d}"
    if state.get("last_digest") == key:
        return False
    state["last_digest"] = key
    return True


def build_digest(statuses: list[Status], now: float) -> dict:
    by = {k: [s.device_id for s in statuses if s.state == k] for k in (OK, LATE, DORMANT, NEVER)}
    healthy = not by[LATE]
    # Say exactly what is true. This is the twice-daily reassurance message; if it reads "all devices
    # recording" while a device is dormant, it is training the reader to discount it — which is how a
    # monitor becomes decoration.
    inactive = len(by[DORMANT]) + len(by[NEVER])
    if healthy:
        head = (f"✓ all {len(by[OK])} active devices recording"
                + (f" ({inactive} inactive, listed below)" if inactive else ""))
    else:
        head = f"⚠ {len(by[LATE])} device(s) NOT RECORDING"
    parts = [f"{len(by[OK])}/{len(statuses)} recording"]
    if by[LATE]:
        parts.append(f"NOT RECORDING: {', '.join(by[LATE])}")
    if by[DORMANT]:
        parts.append(f"dormant (>7d, not alerted): {', '.join(by[DORMANT])}")
    if by[NEVER]:
        parts.append(f"registered but never seen: {', '.join(by[NEVER])}")
    return _alert("recording_digest", "info" if healthy else "warning", "_roster",
                  head + " — " + " | ".join(parts),
                  ok=len(by[OK]), late=by[LATE], dormant=by[DORMANT], never=by[NEVER],
                  ts=datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


# ── I/O ─────────────────────────────────────────────────────────────────────────────────────────────────
def load_expectations(path: Path = CONFIG) -> Expectations:
    raw = yaml.safe_load(path.read_text()) if path.exists() else {}
    raw = raw or {}
    return Expectations(
        default_stale_s=float(raw.get("default_stale_min", 60)) * 60,
        dormant_after_s=float(raw.get("dormant_after_days", 7)) * 86400,
        per_device={k: float(v) * 60 for k, v in (raw.get("per_device_stale_min") or {}).items()},
        per_type={k: float(v) * 60 for k, v in (raw.get("per_type_stale_min") or {}).items()},
        ignore=set(raw.get("ignore") or []),
    )


def _epoch(ts: str) -> float | None:
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def read_roster() -> list[Row]:
    """device_last_seen is the spine (it RETAINS dead devices). The registry adds anything that has never
    reported at all — which device_last_seen cannot know about by construction."""
    rows: dict[str, Row] = {}
    if DB.exists():
        try:
            con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
            for did, dtype, area, ts in con.execute(
                    "SELECT device_id, device_type, area, last_ts FROM device_last_seen"):
                rows[did] = Row(did, dtype, area, _epoch(ts))
            con.close()
        except sqlite3.Error as exc:
            _log(f"device_last_seen read failed: {exc}")
    try:
        reg = (yaml.safe_load(REGISTRY.read_text()) or {}).get("devices", {}) or {}
    except (OSError, yaml.YAMLError):
        reg = {}
    for _key, info in reg.items():
        did = (info or {}).get("device_id")
        if did and did not in rows:
            rows[did] = Row(did, (info or {}).get("device_type"), (info or {}).get("area"), None)
    return list(rows.values())


def _mqtt():
    sys.path.insert(0, str(REPO))
    import paho.mqtt.client as mqtt
    from server.util.mqtt_creds import apply_credentials
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    apply_credentials(c)
    c.connect(BROKER, BROKER_PORT, keepalive=10)
    c.loop_start()
    return c


def publish(alerts: list[dict], statuses: list[Status], dry: bool) -> None:
    beacon = {"ts": int(time.time()),
              "counts": {k: sum(1 for s in statuses if s.state == k) for k in (OK, LATE, DORMANT, NEVER)},
              "not_recording": [s.device_id for s in statuses if s.state == LATE]}
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
        c.publish("home/_recording/status", json.dumps(beacon), qos=1, retain=True)
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


def check_once(dry: bool, force_digest: bool = False) -> int:
    exp = load_expectations()
    roster = read_roster()
    if not roster:
        _log("EMPTY ROSTER — no device_last_seen rows and no registry; refusing to report 'all clear'")
        return 1
    now = time.time()
    statuses = classify(roster, exp, now)
    state = {} if dry else _load_state()
    alerts = diff_alerts(statuses, state)

    hours = [int(h) for h in (yaml.safe_load(CONFIG.read_text()) or {}).get("digest_hours_utc", [8, 20])] \
        if CONFIG.exists() else [8, 20]
    if force_digest or digest_due(now, hours, state):
        alerts.append(build_digest(statuses, now))

    counts = {k: sum(1 for s in statuses if s.state == k) for k in (OK, LATE, DORMANT, NEVER)}
    _log(f"roster {len(statuses)}: ok={counts[OK]} late={counts[LATE]} dormant={counts[DORMANT]} "
         f"never={counts[NEVER]}")
    for s in statuses:
        if s.state != OK:
            age = f"{s.age_s / 3600:.1f}h" if s.age_s is not None else "never"
            _log(f"  {s.state.upper():7} {s.device_id} ({s.area or '?'}) age={age} "
                 f"thr={s.threshold_s / 3600:.1f}h")

    publish(alerts, statuses, dry)
    if not dry:
        _save_state(state)
    return 1 if counts[LATE] else 0


def main() -> int:
    p = argparse.ArgumentParser(description="Alert when a registered device stops recording")
    p.add_argument("--dry-run", action="store_true", help="evaluate + log, publish nothing, keep no state")
    p.add_argument("--digest", action="store_true", help="force the roster digest this run")
    p.add_argument("--status", action="store_true", help="print persisted state and exit")
    a = p.parse_args()
    if a.status:
        print(json.dumps(_load_state(), indent=2))
        return 0
    return check_once(a.dry_run, a.digest)


if __name__ == "__main__":
    raise SystemExit(main())
