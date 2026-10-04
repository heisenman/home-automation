#!/usr/bin/env python3
"""Sign + send a command to the Broan ERV node (hvac_c6) over the ADR-0010 signed channel.

  python3 tools/erv_cmd.py mode med       # off | int | low | med | high | turbo  (over RS-485, v7+)
  python3 tools/erv_cmd.py boost 20     # close the OVR contact for 20 min (1..60) — ERV goes to max airflow
  python3 tools/erv_cmd.py boost 0      # release it now
  python3 tools/erv_cmd.py stats        # force a sniff + register census + telemetry publish

Watch:  mosquitto_sub -h 192.168.1.200 -v -t 'home/edge/hvac_c6/#' -t 'home/attic/erv_pm/state'

Reuses tools/shade_cmd.py's signer and its replay-safe (ts, seq) persistence, pointed at this node. The
per-node HA_CMD_SECRET comes from $HA_CMD_SECRET or edge/esp32c6-hvac/main/secrets.h (gitignored).
"""
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
os.environ.setdefault("HA_NODE", "hvac_c6")
if "HA_CMD_SECRET" not in os.environ:
    sh = REPO / "edge/esp32c6-hvac/main/secrets.h"
    m = re.search(r'#define\s+HA_CMD_SECRET\s+"([^"]+)"', sh.read_text()) if sh.exists() else None
    if not m:
        sys.exit(f"no HA_CMD_SECRET ($HA_CMD_SECRET or {sh}) — run tools/enroll_node.py first")
    os.environ["HA_CMD_SECRET"] = m.group(1)

sys.path.insert(0, str(REPO / "tools"))
import shade_cmd  # noqa: E402  (reads HA_NODE / HA_CMD_SECRET at import)


MODES = ("off", "int", "low", "med", "high", "turbo")   # must match kModes[] in the node's on_cmd


def main() -> None:
    a = sys.argv[1:]
    if a[:1] == ["boost"] and len(a) == 2 and a[1].isdigit() and 0 <= int(a[1]) <= 60:
        shade_cmd.send({"op": "erv_boost", "min": int(a[1])})
    elif a[:1] == ["mode"] and len(a) == 2 and a[1] in MODES:
        shade_cmd.send({"op": "erv_mode", "mode": a[1]})
    elif a == ["stats"]:
        shade_cmd.send({"op": "erv_stats"})
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
