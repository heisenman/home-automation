#!/usr/bin/env python3
"""
device_smoke_test — verify a just-added device actually works end-to-end, so "added" means "verified",
never just "configured" (the add-device-flow smoke test; sibling of node_bringup but for devices).

Two gated checks against the live dictator API (default :8123):
  SENSOR    GET /devices/<id>/last  -> the device decodes through the mapper into canonical readings,
            and the freshest metric is recent (within --max-age). Non-intrusive.
  ACTUATOR  (opt-in, --command 'trait:action[:k=v,...]') POST /devices/<id>/command with the admin
            bearer + confirm second-factor -> the command round-trips and the control plane ACKs it.
            *Intrusive* — this actuates real hardware, so it only runs when you pass --command.

Auth is derived from the master in-process and never printed: bearer = SHA256("ha-api:"+master),
confirm_pin = SHA256("ha-confirm:"+master). Master comes from --master / $HA_MASTER_PASSPHRASE /
instance/.master_pass.

  python3 tools/device_smoke_test.py meter_h_bed                    # sensor decode check
  python3 tools/device_smoke_test.py --list                        # list known sensor device_ids
  python3 tools/device_smoke_test.py dehumidifier_office \
      --command 'dehumidifier:set:power=off'                       # actuator round-trip (intrusive)
"""
import argparse
import json
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def c(s, color):
    return f"\033[{color}m{s}\033[0m" if sys.stdout.isatty() else s


def gate(ok, label, detail=""):
    mark = c("PASS", "32") if ok else c("FAIL", "31")
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def api_get(url, timeout=6):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode())


def api_post(url, body, bearer, timeout=45):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {bearer}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:                 # the API returns 4xx/5xx with a JSON body
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"status": "error", "reason": e.reason}


def age_secs(ts_iso):
    t = datetime.strptime(ts_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds()


def main():
    p = argparse.ArgumentParser(description="Smoke-test a device end-to-end (decode / command ack)")
    p.add_argument("device_id", nargs="?")
    p.add_argument("--api", default="http://127.0.0.1:8123")
    p.add_argument("--max-age", type=float, default=900.0, help="freshest metric must be within this (s)")
    p.add_argument("--command", help="actuator round-trip: 'trait:action[:k=v,k=v]' (INTRUSIVE)")
    p.add_argument("--master", default=None, help="master passphrase (else env / instance/.master_pass)")
    p.add_argument("--list", action="store_true", help="list known sensor device_ids and exit")
    a = p.parse_args()

    if a.list:
        _, d = api_get(f"{a.api}/api/v1/sensors")
        rows = d if isinstance(d, list) else d.get("sensors", [])
        ids = sorted({r.get("device_id") or r.get("id") for r in rows if (r.get("device_id") or r.get("id"))})
        print("\n".join(ids))
        return
    if not a.device_id:
        p.error("device_id required (or use --list)")

    print(c(f"device_smoke_test: {a.device_id}  (api {a.api})", "1"))
    ok = True

    # ── SENSOR: does it decode through to canonical readings, freshly? ──────────────────────────────
    print(c("\n[1] SENSOR decode", "1;36"))
    is_sensor = False
    try:
        status, last = api_get(f"{a.api}/devices/{a.device_id}/last")
        readings = last.get("readings", [])
        if readings:
            is_sensor = True
            freshest = min(age_secs(r["ts"]) for r in readings)
            for r in sorted(readings, key=lambda r: r["metric"]):
                print(f"      {r['metric']:<16} {r['value']} {r.get('unit','')}  ({age_secs(r['ts'])/60:.1f} min ago)")
            ok &= gate(freshest <= a.max_age, "freshest metric is recent",
                       f"{freshest/60:.1f} min ago (limit {a.max_age/60:.0f})")
        else:
            print("  (no readings — not a sensor, or none ingested yet)")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"  no readings for '{a.device_id}' (unknown sensor, or a pure actuator)")
        else:
            ok &= gate(False, "sensor read-back", f"HTTP {e.code}")

    # ── ACTUATOR: opt-in command round-trip + ack ───────────────────────────────────────────────────
    if a.command:
        print(c("\n[2] ACTUATOR command round-trip (intrusive)", "1;36"))
        sys.path.insert(0, str(REPO))
        from server.control import secret_store as S
        master = S.load_master(a.master)
        bearer, pin = S.api_token(master), S.confirm_token(master)
        parts = a.command.split(":")
        if len(parts) < 2:
            gate(False, "command format", "expected 'trait:action[:k=v,...]'"); sys.exit(1)
        trait, action = parts[0], parts[1]
        args = {}
        if len(parts) > 2 and parts[2]:
            for kv in parts[2].split(","):
                k, _, v = kv.partition("=")
                args[k] = v
        body = {"trait": trait, "action": action, "args": args, "confirm_pin": pin}
        print(f"      POST {a.device_id} <- trait={trait} action={action} args={args}")
        code, payload = api_post(f"{a.api}/devices/{a.device_id}/command", body, bearer)
        print(f"      ack: HTTP {code} {json.dumps(payload)[:200]}")
        ok &= gate(200 <= code < 300 and str(payload.get("status", "")).lower() in ("ok", "applied", "accepted", "noop"),
                   "command ACKed by the control plane", f"status={payload.get('status')}")
    elif not is_sensor:
        print(c("\n[2] ACTUATOR — no --command given", "33"))
        ok &= gate(False, "nothing verified",
                   "no sensor readings AND no --command — unknown device, or a pure actuator (pass --command)")
        print("      pass --command 'trait:action[:k=v]' to round-trip an actuator (actuates real HW).")

    print()
    print(c("✓ smoke test PASSED" if ok else "✗ smoke test FAILED", "1;32" if ok else "1;31"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
