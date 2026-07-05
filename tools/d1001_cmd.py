#!/usr/bin/env python3
"""Sign + send an authority command to the D1001 panel over the ADR-0010 signed channel (roadmap #4).

Since v64-signed-cmd the panel REFUSES unsigned ota/fs/gpio/exp — they must arrive on d1001-beachhead/cmd
as a signed {p,s} envelope. This wraps tools/edge_sign.py with the panel's per-device HA_CMD_SECRET (read
from the gitignored bench secrets.h, or $HA_CMD_SECRET) and publishes to the broker. No secret is stored
in this file.

  # firmware OTA (the new deploy path — replaces `mosquitto_pub -t .../cmd/ota`)
  python3 tools/d1001_cmd.py ota http://192.168.0.112:8001/d1001_beachhead.bin

  # SD file op (JSON as fs_ops expects: ls/stat/read/write/rm/mkdir/df)
  python3 tools/d1001_cmd.py fs '{"cmd":"ls","path":"/"}'

  # drive / read a P4 GPIO or a PCA9535 expander pin
  python3 tools/d1001_cmd.py gpio 22 1      # write; omit value to read
  python3 tools/d1001_cmd.py exp 8          # read expander pin 8

  # roadmap #5: pull a SwitchBot meter's on-device history over GATT (relays home/edge/<node>/<mac>/history)
  python3 tools/d1001_cmd.py gathist B0:E9:FE:54:A8:02 outdoor

Broker + secret source override with $HA_BROKER (default 192.168.0.210) / $HA_CMD_SECRET.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SECRETS_H = REPO / "provisioning/reterminal/beachhead/main/secrets.h"
TOPIC = "d1001-beachhead/cmd"


def load_secret() -> str:
    s = os.environ.get("HA_CMD_SECRET")
    if s:
        return s
    if SECRETS_H.exists():
        m = re.search(r'#define\s+HA_CMD_SECRET\s+"([^"]+)"', SECRETS_H.read_text())
        if m:
            return m.group(1)
    sys.exit("no HA_CMD_SECRET ($HA_CMD_SECRET or the panel secrets.h)")


def build_inner(argv: list[str]) -> dict:
    op = argv[0]
    if op == "ota":
        if len(argv) < 2:
            sys.exit("ota needs a url")
        inner = {"op": "ota", "url": argv[1]}
        if len(argv) >= 3:
            inner["sha256"] = argv[2]      # carried for forward-compat (firmware verify is a follow-up)
        return inner
    if op == "fs":
        if len(argv) < 2:
            sys.exit("fs needs a json op, e.g. '{\"cmd\":\"ls\",\"path\":\"/\"}'")
        inner = json.loads(argv[1])
        inner["op"] = "fs"
        return inner
    if op in ("gpio", "exp"):
        if len(argv) < 2:
            sys.exit(f"{op} needs a pin")
        inner = {"op": op, "pin": int(argv[1])}
        if len(argv) >= 3:
            inner["val"] = int(argv[2])
        return inner
    if op == "beep":                            # roadmap #6: audible test tone (omit args for the chime)
        inner = {"op": "beep"}
        if len(argv) >= 2:
            inner["freq"] = int(argv[1])
        if len(argv) >= 3:
            inner["ms"] = int(argv[2])
        if len(argv) >= 4:
            inner["amp"] = int(argv[3])
        return inner
    if op == "gattprobe":                       # roadmap #5 Spike 0: connect+discover a SwitchBot mac
        if len(argv) < 2:
            sys.exit("gattprobe needs a mac (AA:BB:CC:DD:EE:FF)")
        return {"op": "gattprobe", "mac": argv[1]}
    if op == "gathist":                         # roadmap #5 full pull: relay a meter's on-device history
        if len(argv) < 2:
            sys.exit("gathist needs a mac [profile] [window] "
                     "(profile = meter_pro | outdoor, default outdoor; window = recent-N records, default 1024)")
        inner = {"op": "gathist", "mac": argv[1]}
        if len(argv) >= 3:
            inner["profile"] = argv[2]
        if len(argv) >= 4:
            inner["window"] = int(argv[3])
        return inner
    sys.exit(f"unknown op '{op}' (ota|fs|gpio|exp|gattprobe|gathist|beep)")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sys.path.insert(0, str(REPO / "tools"))
    import edge_sign

    env = edge_sign.wrap(build_inner(sys.argv[1:]), secret=load_secret())
    broker = os.environ.get("HA_BROKER", "192.168.0.210")
    payload = json.dumps(env)
    subprocess.run(["mosquitto_pub", "-h", broker, "-t", TOPIC, "-m", payload], check=True)
    print(f"signed + sent to {broker} {TOPIC}: {env['p']}")


if __name__ == "__main__":
    main()
