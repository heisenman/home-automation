#!/usr/bin/env python3
"""Fan-only zero blocks for the dehumidifier study (Hugh, 2026-11-02: 10:00 and 15:00 PT, 30 min, until G2a's
HVAC zero passes). Runs ON ha-2, piped by tools/fan_block.sh from .210's crontab:

    ssh 192.168.1.200 'cd ~/home_automation && venv/bin/python - start|stop|status' < tools/fan_block.py

The furnace blower (hvac_blower, Venstar fan setting) goes ON with the Aprilaire and heat idle, so the HVAC study
pair sees blower air with nothing but the ERV mixed in — the state normal operation never produces, since the
blower only runs for heat calls or with the Aprilaire (study doc §6b, 2026-11-02).

Commands go through the same control-API path as a PWA press (POST /devices/hvac_blower/command), so the
issuer's ack, logging and safety apply. Rules:
  start  skip if the Aprilaire is running, a heat call is on, the fan is already ON (a person's setting — not
         ours), or telemetry is stale. Otherwise fan ON, and a marker records that we did it.
  stop   only if the marker says we started it. If the fan was switched back to Auto by someone, just clear the
         marker. If the Aprilaire started meanwhile, leave the blower on and hand it to the follow coupling
         (engaged = True) so it goes off when the Aprilaire stops, exactly as if the follow had turned it on.
         Otherwise fan back to Auto.
"""
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
API = "http://127.0.0.1:8123"
HOT, CONTROL = "instance/db/hot.db", "instance/db/control.db"
MARKER = Path("instance/study_fan_block.json")
FRESH_S = 300
APR_ON_W = 30


def latest(dev, metric):
    c = sqlite3.connect(f"file:{HOT}?mode=ro", uri=True)
    # readings, not latest_readings: the Venstar thermostat is a non-authoritative self-report and only lands there
    r = c.execute("SELECT value, ts FROM readings WHERE device_id=? AND metric=? ORDER BY ts DESC LIMIT 1",
                  (dev, metric)).fetchone()
    if not r:
        return None
    v, ts = r
    t = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    return v if time.time() - t <= FRESH_S else None


def state():
    return {"apr_w": latest("dehum_pm", "power_w"), "heat": latest("thermostat_hall", "hvac_state"),
            "fan_on": latest("hvac_blower", "fan_on_setting"), "running": latest("hvac_blower", "running")}


def fan(on):
    from server.control import secret_store as S
    master = S.load_master(None)
    body = {"trait": "switchable", "action": "set", "args": {"on": bool(on)}, "confirm_pin": S.confirm_token(master)}
    req = urllib.request.Request(f"{API}/devices/hvac_blower/command", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {S.api_token(master)}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {"status": "error", "reason": str(e.reason)}


def log(msg, **kw):
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "msg": msg, **kw}), flush=True)


def start():
    s = state()
    if None in (s["apr_w"], s["heat"], s["fan_on"]):
        return log("skip: stale telemetry", **s)
    if s["apr_w"] > APR_ON_W:
        return log("skip: Aprilaire running", **s)
    if s["heat"] >= 1:
        return log("skip: heat call", **s)
    if s["fan_on"] == 1:
        return log("skip: fan already ON (not ours)", **s)
    code, resp = fan(True)
    if code == 200:
        MARKER.write_text(json.dumps({"started": time.time()}))
    log("start: fan ON" if code == 200 else "start: command failed", code=code, resp=resp, **s)


def stop():
    if not MARKER.exists():
        return log("stop: no block of ours running")
    s = state()
    if s["fan_on"] == 0:
        MARKER.unlink()
        return log("stop: fan already back on Auto (someone switched it); marker cleared", **s)
    if s["apr_w"] is not None and s["apr_w"] > APR_ON_W:
        # hand the blower to the follow coupling once the controller has seen the Aprilaire start
        from server.control import control_store as store
        c = sqlite3.connect(CONTROL)
        try:
            for _ in range(10):
                st = store.get_setting(c, "follow:hvac_blower") or {}
                if st.get("leader_on"):
                    store.set_setting(c, "follow:hvac_blower", {**st, "engaged": True, "at": time.time()})
                    MARKER.unlink()
                    return log("stop: Aprilaire running; blower handed to the follow coupling", **s)
                time.sleep(30)
        finally:
            c.close()
        return log("stop: Aprilaire running but the controller hasn't seen it start; blower left ON — retry stop",
                   **s)
    code, resp = fan(False)
    if code == 200:
        MARKER.unlink()
    log("stop: fan back to Auto" if code == 200 else "stop: command failed", code=code, resp=resp, **s)


if __name__ == "__main__":
    verb = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"start": start, "stop": stop}.get(verb, lambda: log("status", marker=MARKER.exists(), **state()))()
