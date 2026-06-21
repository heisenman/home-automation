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


def make_router(issuer: CommandIssuer, confirm_verifier=None):
    """Build the FastAPI router (imported lazily so non-API code/tests don't need fastapi)."""
    from fastapi import APIRouter, Request, Response

    router = APIRouter(prefix="/devices", tags=["control"])

    @router.post("/{device_id}/command")
    async def post_command(device_id: str, request: Request, response: Response):
        body = await request.json()
        code, payload = handle_command(issuer, device_id, body, confirm_verifier)
        response.status_code = code
        return payload

    return router
