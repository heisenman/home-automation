"""Control API — human/client → server command requests (plan §13.2).

A thin HTTP shim over the PEP (issuer): clients may only *request*; the server authorises (policy),
signs, sends, and reconciles. This router is **NOT mounted** into the live read-only dashboard API yet
— exposing control on the currently-unauthenticated API would re-open the gap we are closing. It mounts
only AFTER broker auth + API auth are in place (see docs/FOLLOWUPS.md / broker-auth-cutover.md).

The Result→HTTP mapping is a pure function so it is unit-testable without a web server.
"""
from __future__ import annotations

import re
from typing import Any

from server.control.issuer import CommandIssuer, Result

# Result.status -> HTTP status code
_STATUS_HTTP = {
    "ok": 200,
    "mismatch": 409,        # device acked but reported state != intended
    "rejected": 403,        # policy/contract/signature denial
    "unknown-device": 404,
    "no-ack": 504,          # device did not respond
}


def result_to_http(r: Result) -> tuple[int, dict[str, Any]]:
    code = _STATUS_HTTP.get(r.status, 500)
    return code, {
        "status": r.status,
        "reason": r.reason,
        "intended": r.intended,
        "reported": r.reported,
        "cmd_id": r.cmd_id,
    }


def handle_command(issuer: CommandIssuer, device_id: str, body: dict[str, Any],
                   confirm_verifier=None) -> tuple[int, dict]:
    """Validate a command-request body and run it through the PEP. Pure (no HTTP framework).

    The second factor for sensitive actions is SOFTWARE (per Hugh 2026-06-21: not all endpoints have
    buttons) — a `confirm_pin` in the body checked by `confirm_verifier(device_id, pin) -> bool`.
    With no verifier configured, sensitive actions cannot be confirmed (fail-safe: policy denies them)."""
    trait = body.get("trait")
    action = body.get("action")
    if not isinstance(trait, str) or not isinstance(action, str):
        return 400, {"status": "bad-request", "reason": "trait and action are required strings"}
    args = body.get("args") or {}
    if not isinstance(args, dict):
        return 400, {"status": "bad-request", "reason": "args must be an object"}
    confirmed = False
    pin = body.get("confirm_pin")
    if pin is not None and confirm_verifier is not None:
        confirmed = bool(confirm_verifier(device_id, str(pin)))
    r = issuer.issue(device_id=device_id, trait=trait, action=action, args=args, confirmed=confirmed)
    return result_to_http(r)


# ── Manual override (user-initiated timeout) ─────────────────────────────────────
# The override is the human escape hatch over the automation loop (ADR-0011 precedence layer 2):
# "off" parks the device, "boost_on" forces it, both with a TTL so a forgotten override self-clears;
# "clear" hands control straight back to the rule. The API only WRITES control.db's override row — the
# controller reads it every tick (store.get_override) and the resolver enforces it (incl. min-off/safety),
# so there is no separate command path and a stale API can't strand the device ON.
_OVERRIDE_ACTIONS = ("off", "boost_on", "clear")
_MAX_OVERRIDE_MIN = 1440          # 24h cap — an override is a timeout, never a permanent mode


def handle_override(conn, device_id: str, body: dict[str, Any], now: float,
                    valid_devices=None) -> tuple[int, dict]:
    """Set or clear a manual override on control.db. Pure (no HTTP framework). `valid_devices`, if
    given, restricts to known controllable device_ids (else 404)."""
    from server.control import control_store as store
    if valid_devices is not None and device_id not in valid_devices:
        return 404, {"status": "unknown-device", "reason": f"no controllable device {device_id!r}"}
    action = body.get("action")
    if action not in _OVERRIDE_ACTIONS:
        return 400, {"status": "bad-request", "reason": f"action must be one of {_OVERRIDE_ACTIONS}"}
    if action == "clear":
        store.clear_override(conn, device_id)
        return 200, {"status": "ok", "device_id": device_id, "override": None}
    dur = body.get("duration_min")
    if isinstance(dur, bool) or not isinstance(dur, (int, float)) or dur <= 0:
        return 400, {"status": "bad-request", "reason": "duration_min must be a positive number"}
    if dur > _MAX_OVERRIDE_MIN:
        return 400, {"status": "bad-request",
                     "reason": f"duration_min exceeds {_MAX_OVERRIDE_MIN} (24h) cap"}
    expiry = now + float(dur) * 60.0
    store.set_override(conn, device_id, action, expiry)
    return 200, {"status": "ok", "device_id": device_id,
                 "override": {"action": action, "expiry": expiry, "duration_min": dur}}


def read_control_state(conn, device_id: str, now: float) -> dict:
    """Compose a device's live control snapshot (policy + active override + recent decisions) from
    control.db. Pure; shared by the admin GET and the display view-model (BFF)."""
    from server.control import control_store as store
    ov = store.get_override(conn, device_id, now)
    override = None
    if ov:
        action, expiry = ov
        override = {"action": action, "expiry": expiry,
                    "expires_in_min": None if expiry is None else max(0.0, (expiry - now) / 60.0)}
    log_rows = store.recent_log(conn, device_id, limit=10)
    return {
        "device_id": device_id,
        "policy": store.get_policy(conn, device_id),
        "override": override,
        "last_decision": log_rows[0] if log_rows else None,
        "recent_log": log_rows,
    }


# ── Policy editing (app-mutable settings) ────────────────────────────────────────
_ALLOWED_STRATEGIES = ("hysteresis", "setpoint")
_WINDOW_RE = re.compile(r"^\d{1,2}:\d{2}-\d{1,2}:\d{2}$")


def _is_num(v) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float))


def handle_policy_update(conn, device_id: str, body: dict[str, Any],
                         valid_devices=None) -> tuple[int, dict]:
    """MERGE-patch a device's automation policy (the app-mutable thresholds/enable/quiet-window). Pure.
    A merge (not replace) so a partial edit never silently drops fields the UI didn't send. The controller
    re-reads the policy every tick, so edits take effect on the next cycle."""
    from server.control import control_store as store
    if valid_devices is not None and device_id not in valid_devices:
        return 404, {"status": "unknown-device", "reason": f"no controllable device {device_id!r}"}
    pol = store.get_policy(conn, device_id)
    if pol is None:
        return 404, {"status": "unknown-device", "reason": f"no policy for {device_id!r}"}
    patch = body or {}

    def bad(reason):
        return 400, {"status": "bad-request", "reason": reason}

    if "enabled" in patch:
        if not isinstance(patch["enabled"], bool):
            return bad("enabled must be a boolean")
        pol["enabled"] = patch["enabled"]
    if "source_sensor" in patch:
        if not isinstance(patch["source_sensor"], str) or not patch["source_sensor"]:
            return bad("source_sensor must be a non-empty string")
        pol["source_sensor"] = patch["source_sensor"]
    if "sensor_stale_min" in patch:
        if not _is_num(patch["sensor_stale_min"]) or patch["sensor_stale_min"] <= 0:
            return bad("sensor_stale_min must be a positive number")
        pol["sensor_stale_min"] = patch["sensor_stale_min"]
    if "control" in patch:
        cp = patch["control"]
        if not isinstance(cp, dict):
            return bad("control must be an object")
        c = dict(pol.get("control", {}))
        for k in ("strategy", "on_above", "off_below", "min_on_min", "min_off_min"):
            if k in cp:
                c[k] = cp[k]
        if c.get("strategy") not in _ALLOWED_STRATEGIES:
            return bad(f"strategy must be one of {_ALLOWED_STRATEGIES}")
        for k in ("on_above", "off_below", "min_on_min", "min_off_min"):
            if k in c and not _is_num(c[k]):
                return bad(f"control.{k} must be a number")
        if c.get("strategy") == "hysteresis" and float(c.get("on_above", 0)) <= float(c.get("off_below", 0)):
            return bad("on_above must be strictly greater than off_below (deadband)")
        pol["control"] = c
    if "schedule" in patch:
        sched = patch["schedule"]
        if not isinstance(sched, list):
            return bad("schedule must be a list")
        for e in sched:
            if not isinstance(e, dict) or not _WINDOW_RE.match(str(e.get("when", ""))):
                return bad("each schedule entry needs when='HH:MM-HH:MM'")
            if e.get("policy") not in ("off", "auto"):
                return bad("each schedule entry needs policy='off'|'auto'")
        pol["schedule"] = sched

    store.set_policy(conn, device_id, pol)
    return 200, {"status": "ok", "device_id": device_id, "policy": pol}


# ── device meta (R8: user-set friendly name / room / hidden) ─────────────────────
def handle_device_meta(conn, device_id: str, body: dict[str, Any]) -> tuple[int, dict]:
    """Set a device's display overlay (name/room/hidden). Pure. Empty-string name/room clears that label
    (UI falls back to the registry); fields omitted are left unchanged. Works for ANY device_id."""
    from server.control import control_store as store
    patch = body or {}
    name, room, hidden = patch.get("name"), patch.get("room"), patch.get("hidden")
    if name is not None and not isinstance(name, str):
        return 400, {"status": "bad-request", "reason": "name must be a string"}
    if room is not None and not isinstance(room, str):
        return 400, {"status": "bad-request", "reason": "room must be a string"}
    if hidden is not None and not isinstance(hidden, bool):
        return 400, {"status": "bad-request", "reason": "hidden must be a boolean"}
    if name is None and room is None and hidden is None:
        return 400, {"status": "bad-request", "reason": "nothing to set (name/room/hidden)"}
    store.set_device_meta(conn, device_id, name=name, room=room, hidden=hidden)
    return 200, {"status": "ok", "device_id": device_id, "meta": store.get_device_meta(conn, device_id)}


def make_device_meta_router(api_authz, control_db):
    """Admin-gated device overlay editor (prefix /api/v1/devices). Works for any device (sensors too)."""
    import sqlite3

    from fastapi import APIRouter, Body, Depends, Header, HTTPException
    from fastapi.responses import JSONResponse

    from server.control import control_store as store

    router = APIRouter(prefix="/api/v1/devices", tags=["devices"])

    def require_admin(authorization: str | None = Header(default=None)):
        if api_authz is None or not api_authz(authorization):
            raise HTTPException(status_code=401, detail="unauthorized",
                                headers={"WWW-Authenticate": "Bearer"})

    @router.put("/{device_id}/meta", dependencies=[Depends(require_admin)])
    async def put_meta(device_id: str, body: dict = Body(...)):
        c = sqlite3.connect(str(control_db))
        store.ensure_schema(c)
        try:
            code, payload = handle_device_meta(c, device_id, body)
        finally:
            c.close()
        return JSONResponse(status_code=code, content=payload)

    @router.get("/meta")            # open read: lets the UI find hidden devices to un-hide them
    async def get_all_meta():
        c = sqlite3.connect(str(control_db))
        store.ensure_schema(c)
        try:
            return JSONResponse(status_code=200, content={"meta": store.all_device_meta(c)})
        finally:
            c.close()

    return router


def make_override_router(api_authz, control_db, device_ids=None):
    """Admin-gated control router (prefix /control): set/clear the manual override and read the live
    control state. Writes only control.db (the controller's source of truth); same bearer as /devices."""
    import sqlite3
    import time

    from fastapi import APIRouter, Body, Depends, Header, HTTPException
    from fastapi.responses import JSONResponse

    from server.control import control_store as store

    router = APIRouter(prefix="/control", tags=["control"])
    devices = set(device_ids) if device_ids is not None else None

    def require_admin(authorization: str | None = Header(default=None)):
        if api_authz is None or not api_authz(authorization):
            raise HTTPException(status_code=401, detail="unauthorized",
                                headers={"WWW-Authenticate": "Bearer"})

    def _conn():
        c = sqlite3.connect(str(control_db))
        store.ensure_schema(c)
        return c

    @router.get("/auth/check", dependencies=[Depends(require_admin)])
    async def auth_check():
        # 200 only if the admin bearer is valid (the dependency 401s otherwise). Lets the UI confirm a
        # password actually worked at login instead of discovering it on the first failed command.
        return JSONResponse(status_code=200, content={"ok": True})

    @router.post("/{device_id}/override", dependencies=[Depends(require_admin)])
    async def post_override(device_id: str, body: dict = Body(...)):
        c = _conn()
        try:
            code, payload = handle_override(c, device_id, body, time.time(), devices)
        finally:
            c.close()
        return JSONResponse(status_code=code, content=payload)

    @router.get("/{device_id}", dependencies=[Depends(require_admin)])
    async def get_control(device_id: str):
        if devices is not None and device_id not in devices:
            return JSONResponse(status_code=404, content={"status": "unknown-device"})
        c = _conn()
        try:
            state = read_control_state(c, device_id, time.time())
        finally:
            c.close()
        return JSONResponse(status_code=200, content=state)

    @router.put("/{device_id}/policy", dependencies=[Depends(require_admin)])
    async def put_policy(device_id: str, body: dict = Body(...)):
        c = _conn()
        try:
            code, payload = handle_policy_update(c, device_id, body, devices)
        finally:
            c.close()
        return JSONResponse(status_code=code, content=payload)

    return router


def make_router(issuer: CommandIssuer, confirm_verifier=None, api_authz=None):
    """Build the FastAPI control router (fastapi imported lazily so non-API code/tests don't need it).

    `api_authz(authorization_header) -> bool` is the admin gate (bearer = SHA256("ha-api:"+master), see
    secret_store.make_api_token_verifier). It is REQUIRED: with no verifier every request is 401, so the
    control plane can never be exposed unauthenticated even if mounted by mistake. `confirm_verifier`
    remains the SEPARATE second factor for sensitive actions (checked inside handle_command)."""
    from fastapi import APIRouter, Body, Depends, Header, HTTPException
    from fastapi.responses import JSONResponse

    router = APIRouter(prefix="/devices", tags=["control"])

    def require_admin(authorization: str | None = Header(default=None)):
        if api_authz is None or not api_authz(authorization):
            raise HTTPException(status_code=401, detail="unauthorized",
                                headers={"WWW-Authenticate": "Bearer"})

    # NB: take the JSON body as a `dict` param and return a JSONResponse rather than typed
    # Request/Response params — with `from __future__ import annotations` those local-scope types
    # aren't resolvable by FastAPI's hint analysis and get mis-read as query params (422).
    @router.post("/{device_id}/command", dependencies=[Depends(require_admin)])
    async def post_command(device_id: str, body: dict = Body(...)):
        # handle_command can block for seconds (a Midea LAN command shells out to the CLI, up to a 40s
        # subprocess timeout). Run it off the event loop so one device command doesn't stall the whole
        # async API (reads, other clients) while it waits on the appliance.
        from starlette.concurrency import run_in_threadpool
        code, payload = await run_in_threadpool(handle_command, issuer, device_id, body, confirm_verifier)
        return JSONResponse(status_code=code, content=payload)

    return router
