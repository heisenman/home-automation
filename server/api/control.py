"""Control API — human/client → server command requests (plan §13.2).

A thin HTTP shim over the PEP (issuer): clients may only *request*; the server authorises (policy),
signs, sends, and reconciles. This router is **NOT mounted** into the live read-only dashboard API yet
— exposing control on the currently-unauthenticated API would re-open the gap we are closing. It mounts
only AFTER broker auth + API auth are in place (see docs/FOLLOWUPS.md / broker-auth-cutover.md).

The Result→HTTP mapping is a pure function so it is unit-testable without a web server.
"""
from __future__ import annotations

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
        code, payload = handle_command(issuer, device_id, body, confirm_verifier)
        return JSONResponse(status_code=code, content=payload)

    return router
