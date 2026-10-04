#!/usr/bin/env python3
"""Sign + send a command to the Aprilaire node (dehum_c6) over the ADR-0010 signed channel.

  python3 tools/dehum_cmd.py call 20      # close DH (call for dehumidification) for 20 min (1..60); renew to extend
  python3 tools/dehum_cmd.py call 0       # release (deferred to the 60 s minimum on-time if just closed)
  python3 tools/dehum_cmd.py status       # log + publish relay state and lease remaining

Watch:  mosquitto_sub -h 192.168.1.200 -v -t 'home/edge/dehum_c6/#'

A new call within 5 min of the last release is REFUSED by the node (minimum off-time). Reuses
tools/shade_cmd.py's signer and replay-safe (ts, seq) persistence, pointed at this node.
"""
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
os.environ.setdefault("HA_NODE", "dehum_c6")
if "HA_CMD_SECRET" not in os.environ:
    sh = REPO / "edge/esp32c6-dehum/main/secrets.h"
    m = re.search(r'#define\s+HA_CMD_SECRET\s+"([^"]+)"', sh.read_text()) if sh.exists() else None
    if not m:
        sys.exit(f"no HA_CMD_SECRET ($HA_CMD_SECRET or {sh}) — run tools/enroll_node.py first")
    os.environ["HA_CMD_SECRET"] = m.group(1)

sys.path.insert(0, str(REPO / "tools"))
import shade_cmd  # noqa: E402  (reads HA_NODE / HA_CMD_SECRET at import)


def main() -> None:
    a = sys.argv[1:]
    if a[:1] == ["call"] and len(a) == 2 and a[1].isdigit() and 0 <= int(a[1]) <= 60:
        shade_cmd.send({"op": "dehum_call", "min": int(a[1])})
    elif a == ["status"]:
        shade_cmd.send({"op": "dehum_status"})
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
