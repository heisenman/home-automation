#!/usr/bin/env python3
"""Set the ERV's wet trim (server/control/wet_trim.py) on ha-2 and read it back. Piped by tools/wet_trim_set.sh:

    ssh 192.168.1.200 'cd ~/home_automation && venv/bin/python - on STEP_C | off' < tools/wet_trim_set.py

  on STEP_C  enabled, one level per STEP_C °C of outdoor-over-indoor dew point (the other settings are kept)
  off        disabled; the step and other settings are kept
Prints one JSON line; exit 0 only when the read-back matches (the wrapper removes its cron lines on 0).
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


def main(argv):
    want = {"enabled": True, "step_c": float(argv[1])} if argv[0] == "on" else {"enabled": False}
    c, r = api("PUT", f"/control/{ERV}/policy", {"wet_trim": want})
    got = ((r.get("policy") or {}).get("wet_trim") or {}) if c == 200 else {}
    ok = c == 200 and all(got.get(k) == v for k, v in want.items())
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "msg": argv[0], "ok": ok,
                      "code": c, "wet_trim": got or r.get("reason")}), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["off"]))
