#!/usr/bin/env python3
"""Sign + send a command to a PG03V3 smart-plug node (edge/esp32c3-plug, ADR-0042) over ADR-0010.

  python3 tools/plug_cmd.py plug_01 status          # log + publish relay/meter state
  python3 tools/plug_cmd.py plug_01 on|off          # switch the relay (an ON also re-arms an over-current trip)
  python3 tools/plug_cmd.py plug_01 cycle 10        # off, then back on after 10 s (1..120)
  python3 tools/plug_cmd.py plug_01 boot restore    # power-on state: on | off | restore (default)
  python3 tools/plug_cmd.py plug_01 lock on|off     # child lock: ignore the physical button

Watch:  mosquitto_sub -h 192.168.1.200 -v -t 'home/edge/plug_01/#'

The node's secret comes from the encrypted LUT (instance/node_secrets.enc) — one tool for every plug, nothing
printed. Reuses tools/shade_cmd.py's signer and replay-safe (ts, seq) persistence.
"""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))


def main() -> None:
    a = sys.argv[1:]
    if len(a) < 2:
        sys.exit(__doc__)
    node, verb, rest = a[0], a[1], a[2:]
    if verb == "status" and not rest:
        cmd = {"op": "plug_status"}
    elif verb in ("on", "off") and not rest:
        cmd = {"op": "plug_set", "on": verb == "on"}
    elif verb == "cycle" and len(rest) == 1 and rest[0].isdigit() and 1 <= int(rest[0]) <= 120:
        cmd = {"op": "plug_cycle", "off_s": int(rest[0])}
    elif verb == "boot" and rest in (["on"], ["off"], ["restore"]):
        cmd = {"op": "plug_cfg", "boot": rest[0]}
    elif verb == "lock" and rest in (["on"], ["off"]):
        cmd = {"op": "plug_cfg", "lock": rest[0] == "on"}
    else:
        sys.exit(__doc__)

    if "HA_CMD_SECRET" not in os.environ:
        from server.control import secret_store as S
        lut = S.load_lut(REPO / "instance/node_secrets.enc", S.load_master(None))
        ent = lut.get(node)
        sec = ent.get("cmd_secret") if isinstance(ent, dict) else ent
        if not sec:
            sys.exit(f"{node}: not in the node-secret LUT — enroll it first (tools/enroll_node.py)")
        os.environ["HA_CMD_SECRET"] = sec
    os.environ["HA_NODE"] = node
    import shade_cmd  # noqa: E402  (reads HA_NODE / HA_CMD_SECRET at import)
    shade_cmd.send(cmd)


if __name__ == "__main__":
    main()
