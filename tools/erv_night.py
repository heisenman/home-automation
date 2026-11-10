#!/usr/bin/env python3
"""ERV night pair for the dehumidifier study (Hugh, 2026-11-10): one Aprilaire-only night with the ERV OFF (no ERV
moisture term at all: a clean L/kWh reference) and one at HIGH (the most ERV moisture), at similar dew points, so the
difference measures the ERV term during runs. Runs ON ha-2, piped by tools/erv_night.sh from .210's crontab, like
tools/erv_min.py:

    ssh 192.168.1.200 'cd ~/home_automation && venv/bin/python - off|high MIN | guard | stop | status' < tools/erv_night.py

  off MIN   an `off` override for MIN minutes (self-expiring); writes a marker so `guard` knows to watch.
  high MIN  a `boost_on` override for MIN minutes (the controller runs a level-mode boost at the top level).
  guard     while an `off` night is on: if the air-quality index (the mean of the ERV policy's source sensors, as
            the policy aggregates them) is <= GUARD_AQ on two checks in a row, clear the override so the air-
            quality rule takes over again. Ends the night's data early rather than letting the bedrooms go stale.
  stop      clear the override if it's still ours (marker present), remove the marker. Safe to re-run.

Overrides, not policy edits: both actions exist in the override API and expire on their own, so a missed stop
leaves nothing behind.
"""
import calendar
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
API = "http://127.0.0.1:8123"
ERV = "erv_attic"
ACTIONS = {"off": "off", "high": "boost_on"}
GUARD_AQ = 60              # the ERV's own level-3 band edge: at or below it the policy wants the most fresh air
GUARD_STALE_S = 15 * 60
MARKER = Path("instance/study_erv_night.json")
HOT = "instance/db/hot.db"


def api(method, path, body=None):
    from server.control import secret_store as S
    req = urllib.request.Request(f"{API}{path}", method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {S.api_token(S.load_master(None))}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {"status": "error", "reason": str(e.reason)}


def log(msg, **kw):
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "msg": msg, **kw}), flush=True)


def aq_index():
    """(mean air-quality index over the ERV policy's source sensors, n fresh) from latest_readings, or (None, 0)."""
    code, st = api("GET", f"/control/{ERV}")
    sensors = ((st.get("policy") or {}).get("source_sensors") or []) if code == 200 else []
    if not sensors:
        return None, 0
    c = sqlite3.connect(f"file:{HOT}?mode=ro", uri=True)
    rows = c.execute(f"SELECT value, ts FROM latest_readings WHERE metric='air_quality' AND device_id IN "
                     f"({','.join('?' * len(sensors))})", sensors).fetchall()
    now = time.time()
    vals = [v for v, ts in rows
            if now - calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) <= GUARD_STALE_S]
    return (sum(vals) / len(vals) if vals else None), len(vals)


def start(kind, minutes):
    if MARKER.exists() and json.loads(MARKER.read_text())["end"] > time.time():
        return log("start: a night is already on (cron retry), nothing done", marker=json.loads(MARKER.read_text()))
    c, r =api("POST", f"/control/{ERV}/override", {"action": ACTIONS[kind], "duration_min": minutes})
    if c == 200:
        MARKER.write_text(json.dumps({"kind": kind, "started": time.time(), "end": time.time() + minutes * 60,
                                      "low_checks": 0}))
    log("start", kind=kind, minutes=minutes, erv=[c, r.get("override") or r.get("reason")])


def guard():
    if not MARKER.exists():
        return
    m = json.loads(MARKER.read_text())
    if m["kind"] != "off":
        return
    if time.time() > m["end"]:
        MARKER.unlink(missing_ok=True)
        return log("guard: night over, marker removed")
    aq, n = aq_index()
    m["low_checks"] = m["low_checks"] + 1 if aq is not None and aq <= GUARD_AQ else 0
    if m["low_checks"] >= 2:
        c, r = api("POST", f"/control/{ERV}/override", {"action": "clear"})
        if c == 200:
            MARKER.unlink(missing_ok=True)
        return log("guard: air quality low twice, override cleared", aq=round(aq), sensors=n,
                   erv=[c, r.get("status") or r.get("reason")])
    MARKER.write_text(json.dumps(m))
    log("guard", aq=None if aq is None else round(aq), sensors=n, low_checks=m["low_checks"])


def stop():
    if not MARKER.exists():
        return log("stop: no marker, nothing to clear")
    c, r = api("POST", f"/control/{ERV}/override", {"action": "clear"})
    if c == 200:
        MARKER.unlink(missing_ok=True)
    log("stop", erv=[c, r.get("status") or r.get("reason")])


def status():
    _, st = api("GET", f"/control/{ERV}")
    aq, n = aq_index()
    log("status", marker=json.loads(MARKER.read_text()) if MARKER.exists() else None, override=st.get("override"),
        aq=None if aq is None else round(aq), sensors=n)


if __name__ == "__main__":
    a = sys.argv[1:] or ["status"]
    if a[0] in ACTIONS:
        start(a[0], int(a[1]))
    else:
        {"guard": guard, "stop": stop}.get(a[0], status)()
