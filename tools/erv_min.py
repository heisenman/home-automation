#!/usr/bin/env python3
"""Hold the ERV at its minimum (level 1, ~66 cfm) for a dehumidifier-study window (Hugh, 2026-11-09: "set the ERV to
minimum overnight", so the Aprilaire's runs carry the smallest ERV moisture correction). Runs ON ha-2, piped by
tools/erv_min.sh from .210's crontab, like tools/dehum_block.py:

    ssh 192.168.1.200 'cd ~/home_automation && venv/bin/python - start|stop|status' < tools/erv_min.py

  start  saves the ERV's air-quality bands and dry-boost level to a marker, then sets one band (level 1 at any
         air quality) and dry_boost.level 1, so neither the VOC rule nor the dry boost raises it. Re-running start
         keeps the ORIGINAL saved values.
  stop   puts the saved bands and dry-boost level back (dry-boost level 3 if none was saved) and removes the marker.
         Safe to re-run.

Policy edits, not an override: the override API has no "low" action, and an `off` override would stop the ERV.
A policy edit doesn't expire on its own, so the restore is a cron stop with a retry.
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
API = "http://127.0.0.1:8123"
ERV = "erv_attic"
MIN_BANDS = [{"max": None, "level": 1}]
DEFAULT_BOOST_LEVEL = 3
MARKER = Path("instance/study_erv_min.json")


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


def current():
    code, st = api("GET", f"/control/{ERV}")
    pol = (st.get("policy") or {}) if code == 200 else {}
    return code, (pol.get("control") or {}).get("bands"), (pol.get("dry_boost") or {}).get("level")


def start():
    if MARKER.exists():
        saved = json.loads(MARKER.read_text())
    else:
        code, bands, level = current()
        if code != 200 or not bands:
            return log("start: couldn't read the ERV policy, nothing done", code=code)
        saved = {"started": time.time(), "bands": bands, "boost_level": level}
        MARKER.write_text(json.dumps(saved))
    c, r = api("PUT", f"/control/{ERV}/policy", {"control": {"bands": MIN_BANDS}, "dry_boost": {"level": 1}})
    log("start", erv=[c, r.get("status") or r.get("reason")], saved=saved)


def stop():
    if not MARKER.exists():
        return log("stop: no marker, nothing to restore")
    saved = json.loads(MARKER.read_text())
    c, r = api("PUT", f"/control/{ERV}/policy", {"control": {"bands": saved["bands"]},
                                                 "dry_boost": {"level": saved.get("boost_level") or DEFAULT_BOOST_LEVEL}})
    if c == 200:
        MARKER.unlink(missing_ok=True)
    log("stop", erv=[c, r.get("status") or r.get("reason")], restored=saved)


def status():
    _, bands, level = current()
    log("status", marker=json.loads(MARKER.read_text()) if MARKER.exists() else None, bands=bands, boost_level=level)


if __name__ == "__main__":
    {"start": start, "stop": stop}.get(sys.argv[1] if len(sys.argv) > 1 else "status", status)()
