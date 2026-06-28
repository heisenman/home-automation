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
    """Compose a device's live control snapshot (policy + active override + active house scene + recent
    decisions) from control.db. Pure; shared by the admin GET and the display view-model (BFF)."""
    from server.control import control_store as store
    from server.control.automation import apply_scene
    ov = store.get_override(conn, device_id, now)
    override = None
    if ov:
        action, expiry = ov
        override = {"action": action, "expiry": expiry,
                    "expires_in_min": None if expiry is None else max(0.0, (expiry - now) / 60.0)}
    log_rows = store.recent_log(conn, device_id, limit=10)
    pol = store.get_policy(conn, device_id)
    scene = store.get_scene(conn)
    # what the active scene does to THIS device right now (so the UI can show "Away: relaxed / parks it")
    scene_active = None
    if pol is not None:
        eff, scene_off = apply_scene(pol, scene)
        patch = (pol.get("scenes") or {}).get(scene) or {}
        scene_active = {"scene": scene, "off": scene_off,
                        "control": eff.get("control") if eff is not pol else None,
                        "patch": patch or None}
    return {
        "device_id": device_id,
        "policy": pol,
        "override": override,
        "scene": scene,
        "scene_active": scene_active,
        "last_decision": log_rows[0] if log_rows else None,
        "recent_log": log_rows,
    }


# ── House scene (Home/Away/Sleep) ────────────────────────────────────────────────
from server.control.automation import HOUSE_SCENES                       # noqa: E402

_SCENE_NUM_KEYS = ("on_above", "off_below", "min_on_min", "min_off_min")
_SCENE_KEYS = {"off", *_SCENE_NUM_KEYS}


def handle_set_scene(conn, body: dict[str, Any]) -> tuple[int, dict]:
    """Set the whole-house scene (one global row in control.db). The controller reads it every tick and
    folds each device's matching scene patch into the effective policy. Pure (no HTTP framework)."""
    from server.control import control_store as store
    scene = (body or {}).get("scene")
    if scene not in HOUSE_SCENES:
        return 400, {"status": "bad-request", "reason": f"scene must be one of {list(HOUSE_SCENES)}"}
    store.set_scene(conn, scene)
    return 200, {"status": "ok", **store.get_scene_full(conn), "scenes": list(HOUSE_SCENES)}


def _validate_scenes(sc, bad):
    """Validate a policy `scenes` map: {scene_name: {off?: bool, on_above?/off_below?/min_*?: num}}.
    Returns None if OK, else the (code, body) tuple from `bad`."""
    if not isinstance(sc, dict):
        return bad("scenes must be an object")
    for name, prof in sc.items():
        if name not in HOUSE_SCENES:
            return bad(f"scene name must be one of {list(HOUSE_SCENES)}")
        if not isinstance(prof, dict):
            return bad(f"scene {name!r} must be an object")
        extra = set(prof) - _SCENE_KEYS
        if extra:
            return bad(f"scene {name!r} has unknown keys {sorted(extra)}")
        if "off" in prof and not isinstance(prof["off"], bool):
            return bad(f"scene {name!r}.off must be a boolean")
        for k in _SCENE_NUM_KEYS:
            if k in prof and not _is_num(prof[k]):
                return bad(f"scene {name!r}.{k} must be a number")
        if ("on_above" in prof and "off_below" in prof
                and float(prof["on_above"]) <= float(prof["off_below"])):
            return bad(f"scene {name!r}: on_above must be strictly greater than off_below")
    return None


# ── Policy editing (app-mutable settings) ────────────────────────────────────────
_ALLOWED_STRATEGIES = ("hysteresis", "setpoint", "threshold_ranged")
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
    if "fallback_sensors" in patch:
        fb = patch["fallback_sensors"]
        if not isinstance(fb, list) or any(not isinstance(x, str) or not x for x in fb):
            return bad("fallback_sensors must be a list of non-empty strings")
        pol["fallback_sensors"] = fb
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
        # threshold_ranged: sensor band -> fan speed. Validate the bands (ascending max cutoffs; the final
        # catch-all band has max=null). Editing these is how the purifier's PM2.5->speed map is tuned.
        if "bands" in cp:
            bands = cp["bands"]
            if not isinstance(bands, list) or not bands:
                return bad("control.bands must be a non-empty list")
            prev = None
            for b in bands:
                if not isinstance(b, dict) or not _is_num(b.get("level")):
                    return bad("each band needs a numeric level")
                mx = b.get("max")
                if mx is not None:
                    if not _is_num(mx):
                        return bad("band max must be a number or null")
                    if prev is not None and float(mx) <= float(prev):
                        return bad("band max thresholds must strictly increase")
                    prev = mx
            c["bands"] = bands
        if c.get("strategy") == "threshold_ranged" and not c.get("bands"):
            return bad("threshold_ranged requires control.bands")
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
    if "scenes" in patch:
        err = _validate_scenes(patch["scenes"], bad)
        if err is not None:
            return err
        pol["scenes"] = patch["scenes"]

    store.set_policy(conn, device_id, pol)
    return 200, {"status": "ok", "device_id": device_id, "policy": pol}


# ── device meta (R8: user-set friendly name / room / hidden) ─────────────────────
def handle_device_meta(conn, device_id: str, body: dict[str, Any]) -> tuple[int, dict]:
    """Set a device's display overlay (name/room/hidden). Pure. Empty-string name/room clears that label
    (UI falls back to the registry); fields omitted are left unchanged. Works for ANY device_id."""
    from server.control import control_store as store
    patch = body or {}
    name, room, hidden = patch.get("name"), patch.get("room"), patch.get("hidden")
    retired = patch.get("retired")
    if name is not None and not isinstance(name, str):
        return 400, {"status": "bad-request", "reason": "name must be a string"}
    if room is not None and not isinstance(room, str):
        return 400, {"status": "bad-request", "reason": "room must be a string"}
    if hidden is not None and not isinstance(hidden, bool):
        return 400, {"status": "bad-request", "reason": "hidden must be a boolean"}
    if retired is not None and not isinstance(retired, bool):
        return 400, {"status": "bad-request", "reason": "retired must be a boolean"}
    if name is None and room is None and hidden is None and retired is None:
        return 400, {"status": "bad-request", "reason": "nothing to set (name/room/hidden/retired)"}
    store.set_device_meta(conn, device_id, name=name, room=room, hidden=hidden, retired=retired)
    return 200, {"status": "ok", "device_id": device_id, "meta": store.get_device_meta(conn, device_id)}


def handle_device_calibration(conn, device_id: str, body: dict[str, Any]) -> tuple[int, dict]:
    """Set a per-metric DISPLAY offset (added to shown/graphed values; control is unaffected). Pure.
    Body: {metric, offset}. offset=0 clears it."""
    from server.control import control_store as store
    metric, offset = (body or {}).get("metric"), (body or {}).get("offset")
    if not isinstance(metric, str) or not metric:
        return 400, {"status": "bad-request", "reason": "metric must be a non-empty string"}
    if isinstance(offset, bool) or not isinstance(offset, (int, float)):
        return 400, {"status": "bad-request", "reason": "offset must be a number"}
    store.set_calibration(conn, device_id, metric, float(offset))
    return 200, {"status": "ok", "device_id": device_id, "calibration": store.all_calibration(conn).get(device_id, {})}


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

    @router.put("/{device_id}/calibration", dependencies=[Depends(require_admin)])
    async def put_calibration(device_id: str, body: dict = Body(...)):
        c = sqlite3.connect(str(control_db))
        store.ensure_schema(c)
        try:
            code, payload = handle_device_calibration(c, device_id, body)
        finally:
            c.close()
        return JSONResponse(status_code=code, content=payload)

    @router.get("/meta")            # open read: hidden devices (to un-hide) + calibration offsets
    async def get_all_meta():
        c = sqlite3.connect(str(control_db))
        store.ensure_schema(c)
        try:
            return JSONResponse(status_code=200, content={"meta": store.all_device_meta(c),
                                                          "calibration": store.all_calibration(c)})
        finally:
            c.close()

    return router


def make_registry_router(api_authz, devices_path, control_path=None, node_secrets_path=None, master=None):
    """Admin-gated device registration (the add-device flow, ADR-0002 trait registry):
      POST /api/v1/devices          -> append a SENSOR to devices.yaml
      POST /api/v1/control-devices  -> append an ACTUATOR to control.yaml (its command secret is derived
                                       from the owning node's enrolled cmd_secret). Omitted if control_path None.
      POST /api/v1/nodes            -> enrol a NEW node (mint cmd_secret, re-encrypt node_secrets.enc,
                                       return the secret + secrets.h). Omitted unless node_secrets_path+master.
    Separate from the control.db overlay router above."""
    from pathlib import Path

    from fastapi import APIRouter, Body, Depends, Header, HTTPException
    from fastapi.responses import JSONResponse

    from server.device_registry import handle_add_actuator, handle_add_device, handle_enroll_node

    router = APIRouter(prefix="/api/v1", tags=["devices"])

    def require_admin(authorization: str | None = Header(default=None)):
        if api_authz is None or not api_authz(authorization):
            raise HTTPException(status_code=401, detail="unauthorized",
                                headers={"WWW-Authenticate": "Bearer"})

    @router.post("/devices", dependencies=[Depends(require_admin)])
    async def add_device(body: dict = Body(...)):
        code, payload = handle_add_device(Path(devices_path), body)
        return JSONResponse(status_code=code, content=payload)

    if control_path is not None:
        @router.post("/control-devices", dependencies=[Depends(require_admin)])
        async def add_actuator(body: dict = Body(...)):
            code, payload = handle_add_actuator(Path(control_path), body)
            return JSONResponse(status_code=code, content=payload)

    if node_secrets_path is not None and master is not None:
        @router.post("/nodes", dependencies=[Depends(require_admin)])
        async def enroll_node(body: dict = Body(...)):
            code, payload = handle_enroll_node(Path(node_secrets_path), master, body)
            return JSONResponse(status_code=code, content=payload)

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

    @router.post("/house/scene", dependencies=[Depends(require_admin)])
    async def post_house_scene(body: dict = Body(...)):
        # set the whole-house scene (Home/Away/Sleep). Two path segments after /control, so it never
        # collides with the single-segment /control/{device_id} route.
        c = _conn()
        try:
            code, payload = handle_set_scene(c, body)
        finally:
            c.close()
        return JSONResponse(status_code=code, content=payload)

    @router.get("/house/scene", dependencies=[Depends(require_admin)])
    async def get_house_scene():
        c = _conn()
        try:
            return JSONResponse(status_code=200,
                                content={**store.get_scene_full(c), "scenes": list(HOUSE_SCENES)})
        finally:
            c.close()

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
