"""Cluster state helpers — pure (no FastAPI, no MQTT), shared by the HTTP RPC router
(`server/api/cluster.py`) and the heartbeat publisher (`server/cluster/heartbeat.py`).

Part of the 210↔.245 failover bus (failover/README.md, ADR-0001/0011). Reports THIS box's view:
its role, whether it's running the controller, and whether it holds the floating VIP.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

CONTROLLER_UNIT = os.environ.get("HA_CONTROLLER_UNIT", "ha-controller")
_DEFAULTS = {"ROLE": "primary", "VIP": "192.168.0.200"}


def read_cluster_env(path: str = "instance/cluster.env") -> dict:
    """Parse instance/cluster.env (KEY=value, # comments). Missing file -> defaults."""
    out = dict(_DEFAULTS)
    p = Path(path)
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.split("#")[0].strip()
    return out


def node_id(role: str) -> str:
    """Stable node label for the cluster topic namespace (ha/cluster/<node>/...)."""
    return "210" if role == "primary" else "245"


def controller_active(unit: str = CONTROLLER_UNIT) -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", unit],
                           capture_output=True, text=True, timeout=4)
        return r.stdout.strip() == "active"
    except Exception:
        return False


def vip_held(vip: str) -> bool:
    try:
        r = subprocess.run(["ip", "-o", "addr", "show"], capture_output=True, text=True, timeout=4)
        return vip in r.stdout
    except Exception:
        return False


def cluster_status() -> dict:
    """This box's cluster view. `healthy` is a coarse proxy (controller_active); the authoritative
    fitness gate is the bash `failover/healthcheck.sh` (API up + Midea reachable) used by keepalived."""
    env = read_cluster_env()
    role = env.get("ROLE", "primary")
    active = controller_active()
    return {
        "node": node_id(role),
        "role": role,
        "controller_active": active,
        "vip_held": vip_held(env.get("VIP", "192.168.0.200")),
        "healthy": active,
        "ts": int(time.time()),
    }
