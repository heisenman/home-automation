#!/usr/bin/env python3
"""E1001 battery profiler — the FULL arc, end to end, in code (capture -> LUT -> offsets -> push -> verify).

This is the "long tests must be code-driven" replacement for babysitting the overnight run from an LLM
session. It owns the entire lifecycle so nothing depends on a session being alive at the finish:

  1. CAPTURE   run tools/e1001_overnight_profile.py --run (offset probe + firmware discharge->charge cycle),
               writing the jsonl capture to a DURABLE path (instance/db/e1001-profile/), not ~ scratch.
  2. FIT       tools/e1001_profile.py e1001lut <capture> --write-json <profile> --version v2  -> V->SoC LUT.
  3. OFFSETS   inject the measured charging/usb offsets (default = the run-1 salvaged off_charging=14 mV,
               the only clean mid-SoC datum we have; this run's Phase-A offsets are skipped at high SoC).
  4. PUSH      tools/d1001_profile_push.py --node e1001-c-office --from-json <profile> --push  (data, no reflash).
  5. VERIFY    re-read the retained <node>/profile and confirm source flipped off "default" to the pushed v2.
  6. SIGNAL    publish <node>/profile/status = done (retained) + write a done-file, so anyone who wants to know
               when it finished reads a topic/file instead of holding a session open watching it.

Run detached as the oneshot ha-e1001-profile.service (its own cgroup => survives any session/tool that started
it). tools/e1001_charge.py --then-profile starts that unit once the cell is charged.

    venv/bin/python tools/e1001_profile_run.py --run          # required flag: this drives a real battery cycle
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = "e1001-c-office"
DURABLE = ROOT / "instance" / "db" / "e1001-profile"
PY = str(ROOT / "venv" / "bin" / "python3")


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str):
    print(f"[{utc()}] {msg}", flush=True)


def newest(pattern: str, after: float) -> Path | None:
    cands = [p for p in DURABLE.glob(pattern) if p.stat().st_mtime >= after - 1]
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def publish(broker: str, topic: str, payload: str, retain: bool = True):
    cmd = ["mosquitto_pub", "-h", broker, "-t", topic, "-m", payload]
    if retain:
        cmd.append("-r")
    try:
        subprocess.run(cmd, check=False, timeout=15)
    except Exception as e:  # noqa: BLE001
        log(f"publish {topic} failed: {e}")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    log("$ " + " ".join(cmd))
    return subprocess.run(cmd, **kw)


def main() -> int:
    ap = argparse.ArgumentParser(description="E1001 end-to-end battery profile (capture->fit->push->verify)")
    ap.add_argument("--broker", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--load", type=int, default=3, choices=[0, 1, 2, 3])
    ap.add_argument("--max-hours", type=float, default=14.0)
    ap.add_argument("--off-charging-mv", type=int, default=14,
                    help="charging offset to bake into the v2 profile (default = run-1 salvaged mid-SoC datum)")
    ap.add_argument("--off-usb-mv", type=int, default=0,
                    help="usb-present offset (default 0 — uncharacterized on this panel)")
    ap.add_argument("--version", default="v2")
    ap.add_argument("--capture-only", action="store_true", help="stop after CAPTURE (no fit/push)")
    ap.add_argument("--run", action="store_true", help="required: actually drive a real battery cycle")
    args = ap.parse_args()
    if not args.run:
        print("refusing to run without --run (this drives a real battery cycle)", file=sys.stderr)
        return 2

    DURABLE.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()
    status_topic = f"{NODE}/profile/status"
    publish(args.broker, status_topic, json.dumps({"state": "capturing", "at": utc()}))
    t_start = time.time()

    # 1. CAPTURE (reuse the validated overnight profiler; durable out-dir)
    cap = run([PY, str(ROOT / "tools" / "e1001_overnight_profile.py"),
               "--broker", args.broker, "--port", str(args.port),
               "--load", str(args.load), "--max-hours", str(args.max_hours),
               "--out-dir", str(DURABLE), "--run"])
    log(f"capture exit={cap.returncode} (0=complete, 3=aborted-with-partial)")
    jsonl = newest("e1001-profile-*.jsonl", t_start)
    if jsonl is None:
        log("!! no capture jsonl produced — cannot fit. aborting.")
        publish(args.broker, status_topic, json.dumps({"state": "failed", "why": "no_capture", "at": utc()}))
        return 1
    log(f"capture -> {jsonl}")
    if args.capture_only:
        publish(args.broker, status_topic, json.dumps({"state": "captured", "capture": str(jsonl), "at": utc()}))
        return 0

    # 2. FIT -> V->SoC LUT + seed profile json
    profile = DURABLE / f"battery_profile_{args.version}_{today}.json"
    fit = run([PY, str(ROOT / "tools" / "e1001_profile.py"), "e1001lut", str(jsonl),
               "--write-json", str(profile), "--version", args.version, "--date", today],
              capture_output=True, text=True)
    sys.stdout.write(fit.stdout or "")
    sys.stderr.write(fit.stderr or "")
    if not profile.exists():
        log("!! e1001lut did not write a profile (discharge leg too short?) — keeping capture for inspection.")
        publish(args.broker, status_topic,
                json.dumps({"state": "failed", "why": "fit_no_discharge", "capture": str(jsonl), "at": utc()}))
        return 1

    # 3. OFFSETS — bake the measured charging/usb offsets into the seed profile
    prof = json.loads(profile.read_text())
    prof["off_charging_mv"] = args.off_charging_mv
    prof["off_usb_mv"] = args.off_usb_mv
    prof["method"] = "auto-discharge+offsets"
    profile.write_text(json.dumps(prof, indent=2) + "\n")
    log(f"offsets baked: off_charging_mv={args.off_charging_mv} off_usb_mv={args.off_usb_mv}")

    # 4. PUSH (data, no reflash)
    push = run([PY, str(ROOT / "tools" / "d1001_profile_push.py"),
                "--broker", args.broker, "--node", NODE, "--from-json", str(profile), "--push"],
               capture_output=True, text=True)
    sys.stdout.write(push.stdout or "")
    sys.stderr.write(push.stderr or "")

    # 5. VERIFY — the retained profile should now report the pushed version, source != default
    time.sleep(2)
    got = run([PY, str(ROOT / "tools" / "d1001_profile_push.py"),
               "--broker", args.broker, "--node", NODE, "--get"], capture_output=True, text=True)
    out = (got.stdout or "") + (got.stderr or "")
    sys.stdout.write(out)
    ok = (args.version in out) and ("source=default" not in out) and ("source=" in out)
    state = "done" if ok else "pushed-unverified"
    summary = {"state": state, "version": args.version, "capture": str(jsonl), "profile": str(profile),
               "off_charging_mv": args.off_charging_mv, "off_usb_mv": args.off_usb_mv,
               "capture_exit": cap.returncode, "at": utc()}
    (DURABLE / "last-run.json").write_text(json.dumps(summary, indent=2) + "\n")
    publish(args.broker, status_topic, json.dumps(summary))
    log(f"RESULT: {state}  profile={profile.name}")
    return 0 if ok else 5


if __name__ == "__main__":
    raise SystemExit(main())
