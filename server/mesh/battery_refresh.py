"""Battery refresh — drive the edge fleet's on-demand active-scan windows to capture SwitchBot meter
battery% (fd3d scan-response data, invisible to passive scanning). See docs/decisions on the v20-battfix
firmware: a signed `{"op":"batt_refresh","window_s":N}` opens a brief ACTIVE-scan window per node, and the
node's cache backfills battery onto its passive temp/hum stream.

Two callers, one primitive (a brief MESH-WIDE sweep — every meter is heard by whichever node has the best
two-way link, so we don't need to resolve device→node, and it matches the mesh-redundancy design):
  * nightly  — ha-battery-refresh.timer fires once in the 01:00–05:00 quiet window; sweep all nodes,
               lightly staggered so no two nodes scan actively at the same instant (RF-neighbourly).
  * on-demand — the PWA 🔄 button → POST /control/battery/refresh → sweep now (short/near-simultaneous
               for responsiveness). Refreshes the tapped device within seconds (and the rest as a freebie).

Stamped with `ts`/`seq` (2026-10-12). It used to go out WITHOUT ts so a clock-drifted node would still take it,
but a signed command without ts skipped freshness AND anti-replay entirely — a captured one replayed forever.
The firmware now requires ts; batt_refresh keeps its wide 86400 s window, and nodes fall back to the broker host
for NTP, so a drifted clock still passes.
"""
from __future__ import annotations

import json
import time

from pathlib import Path

from server.control.edge_cmd import signed_command   # {p,s} + the node's shared monotonic (ts, seq)

STATE_DIR = Path(__file__).resolve().parents[2] / "instance"   # where every /cmd sender keeps .<node>_cmd_seq

CMD_TOPIC = "home/edge/{node}/cmd"
DEFAULT_WINDOW_S = 20          # a meter re-advertises ~1–2 s; 20 s tolerates a weaker two-way link (bench-tuned)
DEFAULT_STAGGER_S = 10         # nightly: gap between nodes so active windows don't overlap


def refresh_payload(window_s: int = DEFAULT_WINDOW_S) -> dict:
    return {"op": "batt_refresh", "window_s": int(window_s)}


def send_to_node(publish, lut: dict, node: str, window_s: int = DEFAULT_WINDOW_S,
                 state_dir: Path = STATE_DIR) -> bool:
    """Sign a batt_refresh for `node` and publish via `publish(topic, payload_str)`. Returns True if sent
    (False when the node has no enrolled secret — can't sign, so the firmware would reject it anyway).
    Stamped with the node's shared monotonic (ts, seq) like every other /cmd sender (edge_cmd.signed_command):
    it used to go out with NO ts, which the firmware let skip freshness + anti-replay — the gap closed in the
    firmware 2026-10-12 (ts now mandatory), so an unstamped batt_refresh would be refused there."""
    secret = (lut.get(node) or {}).get("cmd_secret")
    if not secret:
        return False
    env = signed_command(node, secret, refresh_payload(window_s), state_dir)
    publish(CMD_TOPIC.format(node=node), json.dumps(env))
    return True


def sweep(publish, lut: dict, *, window_s: int = DEFAULT_WINDOW_S, stagger_s: float = 0.0,
          nodes: list[str] | None = None, sleep=time.sleep, state_dir: Path = STATE_DIR) -> list[str]:
    """Fire batt_refresh at every enrolled node (or the given subset), optionally staggered. Returns the
    list of nodes commanded. `sleep` is injectable for tests."""
    targets = sorted(nodes if nodes is not None else lut.keys())
    sent: list[str] = []
    for i, node in enumerate(targets):
        if send_to_node(publish, lut, node, window_s, state_dir):
            sent.append(node)
            if stagger_s and i < len(targets) - 1:
                sleep(stagger_s)
    return sent
