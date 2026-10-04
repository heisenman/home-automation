"""Signed command envelopes for native-C edge nodes (ADR-0010) — the server-side twin of tools/edge_sign.py.

The firmware (ha_mqtt.c cmd handler) accepts `{p, s}` on `home/edge/<node>/cmd`, where `p` is the literal
JSON it HMACs with its per-node secret, and — when `p` carries `ts` — enforces a freshness window AND a
strictly increasing `(ts, seq)` persisted in NVS. So every sender to a node must agree on one monotonic
counter, or a command sent in the same second as another sender's is dropped as a replay.

`next_stamp` therefore persists the last `(ts, seq)` per node in `instance/.<node>_cmd_seq` — the SAME file
`tools/shade_cmd.py` (and `tools/erv_cmd.py`) use, so the PWA path and the CLI share one counter on a box.
It holds `ts` at wall-clock and bumps `seq` on a same-second collision, never pushing `ts` into the future
(the firmware's freshness window would reject that).
"""
from __future__ import annotations

import fcntl
import time
from pathlib import Path

from server.mesh.coordinator import sign_envelope

STATE_DIR = Path("instance")


def next_stamp(node: str, state_dir: Path = STATE_DIR, now: float | None = None) -> tuple[int, int]:
    """Monotonic (ts, seq) for `node`, persisted across processes and restarts (file-locked)."""
    path = Path(state_dir) / f".{node}_cmd_seq"
    path.parent.mkdir(parents=True, exist_ok=True)
    wall = int(time.time() if now is None else now)
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        last_ts, last_seq = 0, -1
        try:
            last_ts, last_seq = (int(x) for x in f.read().split())
        except ValueError:
            pass
        ts, seq = (wall, 0) if wall > last_ts else (last_ts, last_seq + 1)
        f.seek(0)
        f.truncate()
        f.write(f"{ts} {seq}")
        f.flush()
    return ts, seq


def signed_command(node: str, secret: str, inner: dict, state_dir: Path = STATE_DIR,
                   now: float | None = None) -> dict:
    """`inner` stamped with this node's next (ts, seq) and wrapped in the firmware's `{p, s}` envelope."""
    ts, seq = next_stamp(node, state_dir, now)
    return sign_envelope(secret, {**inner, "ts": ts, "seq": seq})
