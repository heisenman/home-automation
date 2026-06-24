"""Cluster bus HTTP RPC (failover/README.md, ADR-0001/0011) — the on-demand / fencing layer, redundant
with VRRP adverts + SSH `systemctl stop`.

- `GET  /cluster/status` — OPEN (read-only liveness/diagnostics: role, controller_active, vip_held).
- `POST /cluster/demote` — ADMIN BEARER. Stop OUR controller now (a peer fences us by calling this).
- `POST /cluster/claim`  — ADMIN BEARER. Announce a takeover (the actual start is keepalived/notify's job).

Privileged routes are gated by the same admin-bearer verifier as the control plane — a rogue LAN host
must not be able to stand the dictator down. Import-safe (no heavy deps).
"""
from __future__ import annotations

import logging
import subprocess

from fastapi import APIRouter, Depends, Header, HTTPException

from server.cluster.state import CONTROLLER_UNIT, cluster_status, controller_active

log = logging.getLogger("ha.cluster")


def make_cluster_router(api_authz=None) -> APIRouter:
    """Build the /cluster router. If `api_authz` (the admin-bearer bool verifier) is None, only the open
    /cluster/status route is exposed; demote/claim require it. `require_admin` mirrors the control router:
    an OPTIONAL Authorization header (→ 401, not a 422 validation error, when missing/bad)."""
    r = APIRouter(prefix="/cluster", tags=["cluster"])

    def require_admin(authorization: str | None = Header(default=None)):
        if api_authz is None or not api_authz(authorization):
            raise HTTPException(status_code=401, detail="unauthorized")

    @r.get("/status")
    def status() -> dict:
        return cluster_status()

    if api_authz is not None:
        @r.post("/demote", dependencies=[Depends(require_admin)])
        def demote() -> dict:
            was = controller_active()
            try:
                subprocess.run(["sudo", "systemctl", "stop", CONTROLLER_UNIT],
                               capture_output=True, text=True, timeout=15)
            except Exception as e:                       # never raise into the API
                log.warning("cluster /demote stop failed: %s", e)
            active = controller_active()
            log.warning("cluster /demote — was_active=%s now_active=%s", was, active)
            return {"demoted": not active, "was_active": was, "controller_active": active}

        @r.post("/claim", dependencies=[Depends(require_admin)])
        def claim() -> dict:
            st = cluster_status()
            log.warning("cluster /claim announced — local status=%s", st)
            return {"ack": True, "status": st}

    return r
