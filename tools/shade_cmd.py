#!/usr/bin/env python3
"""Sign + send a command to the ESP32-S3 shade node over the ADR-0010 signed channel.

Wraps tools/edge_sign.py with the node's per-device HA_CMD_SECRET (read from the gitignored bench
secrets.h, or $HA_CMD_SECRET) and publishes to home/edge/<node>/cmd. No secret is stored in this file.

  # ── bring-up pin test (ADR-0041) — INTERACTIVE, the way you actually want it ──
  python3 tools/shade_cmd.py pintest          # Enter = next, p = prev, a = again, q = quit
  python3 tools/shade_cmd.py pintest start    # or drive it one shot at a time
  python3 tools/shade_cmd.py pintest next/prev/again/stop

  # ── normal operation ──
  python3 tools/shade_cmd.py shade 1 up        # ch 1..6, or 0 for ALL; up|down|stop|interim
  python3 tools/shade_cmd.py cal 1 25000 22000 # per-shade travel times (up_ms, down_ms) -> NVS

Watch the node's replies with:
  mosquitto_sub -h 192.168.1.200 -v -t 'home/edge/shades_s3/#'

⚠ ANTI-REPLAY. The firmware acts on a command only if (ts, seq) is STRICTLY newer than the last one it
acted on. edge_sign tracks seq IN-PROCESS, whose docstring assumes "across process restarts the clock has
moved on" — true for occasional commands, false for a pin test where you tap `next` several times inside
one second. Every invocation would restamp (ts, seq=0) and the node would reject all but the first as a
replay. So this tool persists (ts, seq) to a small state file and stamps them explicitly. The interactive
mode sidesteps it entirely by being one process.

Broker + node override with $HA_BROKER (default 192.168.1.200) / $HA_NODE (default shades_s3).
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
import edge_sign  # noqa: E402

NODE = os.environ.get("HA_NODE", "shades_s3")
BROKER = os.environ.get("HA_BROKER", "192.168.1.200")
SECRETS_H = REPO / "edge/esp32s3-shades/main/secrets.h"
STATE = REPO / "instance" / f".{NODE}_cmd_seq"      # instance/ is gitignored

PINTEST_CMDS = ("start", "next", "prev", "again", "stop")
SHADE_CMDS = ("up", "down", "stop", "interim")


def load_secret() -> str:
    s = os.environ.get("HA_CMD_SECRET")
    if s:
        return s
    if SECRETS_H.exists():
        m = re.search(r'#define\s+HA_CMD_SECRET\s+"([^"]+)"', SECRETS_H.read_text())
        if m:
            return m.group(1)
    sys.exit(f"no HA_CMD_SECRET ($HA_CMD_SECRET or {SECRETS_H}) — run tools/enroll_node.py first")


def next_stamp() -> tuple[int, int]:
    """Monotonic (ts, seq) that survives process restarts.

    ts is NOT pushed into the future when we collide — the firmware enforces a freshness window, so we
    hold ts and bump seq instead, which is exactly what seq is for.
    """
    now = int(time.time())
    last_ts, last_seq = 0, -1
    try:
        last_ts, last_seq = (int(x) for x in STATE.read_text().split())
    except Exception:
        pass
    ts, seq = (now, 0) if now > last_ts else (last_ts, last_seq + 1)
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(f"{ts} {seq}")
    except OSError as e:
        print(f"warning: could not persist seq ({e}) — rapid repeats may be rejected as replays")
    return ts, seq


def send(inner: dict, quiet: bool = False) -> None:
    ts, seq = next_stamp()
    inner = {**inner, "ts": ts, "seq": seq}
    env = edge_sign.wrap(inner, load_secret())
    topic = f"home/edge/{NODE}/cmd"
    subprocess.run(
        ["mosquitto_pub", "-h", BROKER, "-t", topic, "-m", json.dumps(env, separators=(",", ":"))],
        check=True,
    )
    if not quiet:
        print(f"-> {inner['op']} {inner.get('cmd', '')}  (ts={ts} seq={seq})")


def interactive() -> None:
    print(f"pin test on {NODE} via {BROKER}.  Watch the node in another terminal:\n"
          f"  mosquitto_sub -h {BROKER} -v -t 'home/edge/{NODE}/log'\n")
    print("  Enter = next   p = prev   a = again (re-assert after the 18 s hold)   q = quit\n")
    send({"op": "pintest", "cmd": "start"})
    try:
        while True:
            k = input("[Enter/p/a/q] ").strip().lower()
            if k in ("q", "quit", "exit"):
                break
            send({"op": "pintest", "cmd": {"": "next", "p": "prev", "a": "again"}.get(k, "next")})
    except (EOFError, KeyboardInterrupt):
        print()
    finally:
        send({"op": "pintest", "cmd": "stop"})
        print("stopped — all lines off, planner back in control")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    op, a = sys.argv[1], sys.argv[2:]

    if op == "pintest":
        if not a:
            interactive()
        elif len(a) == 1 and a[0] in PINTEST_CMDS:
            send({"op": "pintest", "cmd": a[0]})
        else:
            sys.exit(f"usage: pintest [{'|'.join(PINTEST_CMDS)}]   (no arg = interactive)")

    elif op == "shade":
        if len(a) != 2 or a[1] not in SHADE_CMDS:
            sys.exit(f"usage: shade <0-6> {'|'.join(SHADE_CMDS)}   (0 = all channels)")
        send({"op": "shade", "ch": int(a[0]), "cmd": a[1]})

    elif op == "cal":
        if len(a) != 3:
            sys.exit("usage: cal <1-6> <up_ms> <down_ms>")
        send({"op": "shade_cal", "ch": int(a[0]), "up_ms": int(a[1]), "down_ms": int(a[2])})

    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
