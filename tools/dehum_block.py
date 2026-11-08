#!/usr/bin/env python3
"""Aprilaire-only block for the dehumidifier study (Hugh, 2026-11-07: run the Aprilaire, not the Midea, through the
humid stretch Mon 11-09 → Wed 11-11, guard-banded; "this is data collection not power economy"; Aprilaire at 44/40
"so it runs about as much as it can"). Runs ON ha-2, piped by tools/dehum_block.sh from .210's crontab:

    ssh 192.168.1.200 'cd ~/home_automation && venv/bin/python - start <end-epoch>|stop|status' < tools/dehum_block.py

  start  Midea parked by an `off` override that expires on its own GUARD_MIN after the planned end, so a missed
         stop can't leave it off for good. Aprilaire thresholds set to 44/40 (its previous control thresholds are
         kept in a marker for stop). Re-running start is safe: it re-asserts both and keeps the saved thresholds.
  stop   clears the Midea override and puts the Aprilaire's thresholds back from the marker (48/45 if the marker is
         gone), then removes the marker. Safe to re-run.

Same control-API path as a PWA edit (POST /control/<id>/override, PUT /control/<id>/policy), so validation and
logging apply. The furnace blower follows the Aprilaire as in production (fan setting Auto once G2a's hold ends).
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
API = "http://127.0.0.1:8123"
MIDEA, APRILAIRE = "dehumidifier_living_room", "dehum_attic"
BLOCK_ON, BLOCK_OFF = 44, 40
DEFAULT_ON, DEFAULT_OFF = 48, 45
GUARD_MIN = 60
MARKER = Path("instance/study_dehum_block.json")


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


def thresholds(device_id):
    code, st = api("GET", f"/control/{device_id}")
    c = ((st.get("policy") or {}).get("control") or {}) if code == 200 else {}
    return code, {"on_above": c.get("on_above"), "off_below": c.get("off_below")}, st.get("override")


def start(end_epoch):
    if end_epoch <= time.time():
        return log("start: end time already past, nothing done", end=end_epoch)
    if MARKER.exists():
        saved = json.loads(MARKER.read_text())["aprilaire"]
    else:
        code, saved, _ = thresholds(APRILAIRE)
        if code != 200 or None in saved.values():
            return log("start: couldn't read the Aprilaire policy, nothing done", code=code)
        MARKER.write_text(json.dumps({"started": time.time(), "end": end_epoch, "aprilaire": saved}))
    dur = round((end_epoch - time.time()) / 60) + GUARD_MIN
    c1, r1 = api("POST", f"/control/{MIDEA}/override", {"action": "off", "duration_min": dur})
    c2, r2 = api("PUT", f"/control/{APRILAIRE}/policy", {"control": {"on_above": BLOCK_ON, "off_below": BLOCK_OFF}})
    log("start", midea_override=[c1, r1.get("override") or r1.get("reason")],
        aprilaire=[c2, r2.get("status"), f"{BLOCK_ON}/{BLOCK_OFF}"], saved=saved)


def stop():
    saved = json.loads(MARKER.read_text())["aprilaire"] if MARKER.exists() else \
        {"on_above": DEFAULT_ON, "off_below": DEFAULT_OFF}
    c1, r1 = api("POST", f"/control/{MIDEA}/override", {"action": "clear"})
    c2, r2 = api("PUT", f"/control/{APRILAIRE}/policy", {"control": saved})
    if c1 == 200 and c2 == 200:
        MARKER.unlink(missing_ok=True)
    log("stop", midea_override=[c1, r1.get("status")], aprilaire=[c2, r2.get("status"), saved])


def status():
    _, a, _ = thresholds(APRILAIRE)
    _, _, ov = thresholds(MIDEA)
    log("status", marker=json.loads(MARKER.read_text()) if MARKER.exists() else None, aprilaire=a, midea_override=ov)


if __name__ == "__main__":
    verb = sys.argv[1] if len(sys.argv) > 1 else "status"
    if verb == "start":
        start(float(sys.argv[2]))
    elif verb == "stop":
        stop()
    else:
        status()
