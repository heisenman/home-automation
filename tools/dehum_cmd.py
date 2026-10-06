#!/usr/bin/env python3
"""Sign + send a command to the Aprilaire node (dehum_c6) over the ADR-0010 signed channel.

  python3 tools/dehum_cmd.py call 20      # close DH (call for dehumidification) for 20 min (1..60); renew to extend
  python3 tools/dehum_cmd.py call 0       # release (deferred to the 60 s minimum on-time if just closed)
  python3 tools/dehum_cmd.py status       # log + publish relay state and lease remaining
  python3 tools/dehum_cmd.py bus          # RS-485 Remote link report: link, unit run state/RH/error (v7+)
  python3 tools/dehum_cmd.py force 360 2 1  # EXPERIMENT (v8+): raw R-frame on=0x02 dryness=0x01 for 360 s
  python3 tools/dehum_cmd.py force 0 0 0    # cancel the force window
  python3 tools/dehum_cmd.py txtest 10    # BENCH ONLY: stream 0x00 on A/B for 10 s (1..30); refused while linked

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
    elif a[:1] == ["txtest"] and len(a) == 2 and a[1].isdigit() and 1 <= int(a[1]) <= 30:
        shade_cmd.send({"op": "apr_txtest", "secs": int(a[1]), "byte": 0})
    elif a[:1] == ["force"] and len(a) == 4 and all(x.isdigit() for x in a[1:]):
        shade_cmd.send({"op": "apr_force", "secs": int(a[1]), "on": int(a[2]), "dryness": int(a[3])})
    elif a == ["bus"]:
        shade_cmd.send({"op": "apr_status"})
    elif a == ["status"]:
        shade_cmd.send({"op": "dehum_status"})
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
