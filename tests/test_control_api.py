"""Tests for the control API shim (server/api/control.py) — Result→HTTP + software confirm."""
from server.api import control as C
from server.control.issuer import CommandIssuer, DeviceCtl, LoopbackTransport, Result
from server.control.policy import PolicyStore
from server.control.sim import SimActuator
from tests._harness import run_module

SECRETS = {"lock_front": "s-lock", "lamp_office": "s-lamp"}
REGISTRY = {
    "lock_front": DeviceCtl("lock_front", "c6-bench", "entry", {"lockable": {}}),
    "lamp_office": DeviceCtl("lamp_office", "c6-bench", "c_office", {"switchable": {}}),
}


def _issuer():
    sims = {d: SimActuator(d, REGISTRY[d].traits_cfg, SECRETS[d]) for d in REGISTRY}
    return CommandIssuer(registry=REGISTRY, secrets=SECRETS, policy=PolicyStore({"version": 1}),
                         transport=LoopbackTransport(sims))


def test_result_to_http_mapping():
    assert C.result_to_http(Result("ok", "ok"))[0] == 200
    assert C.result_to_http(Result("rejected", "x"))[0] == 403
    assert C.result_to_http(Result("unknown-device", "x"))[0] == 404
    assert C.result_to_http(Result("no-ack", "x"))[0] == 504
    assert C.result_to_http(Result("mismatch", "x"))[0] == 409


def test_happy_switch():
    code, body = C.handle_command(_issuer(), "lamp_office",
                                  {"trait": "switchable", "action": "set", "args": {"on": True}})
    assert code == 200 and body["status"] == "ok", body


def test_bad_request():
    code, body = C.handle_command(_issuer(), "lamp_office", {"action": "set"})
    assert code == 400, body


def test_sensitive_without_pin_denied():
    code, body = C.handle_command(_issuer(), "lock_front", {"trait": "lockable", "action": "unlock"})
    assert code == 403 and body["reason"] == "confirm-required", body


def test_sensitive_with_good_pin_allowed():
    verifier = lambda dev, pin: pin == "1234"
    code, body = C.handle_command(_issuer(), "lock_front",
                                  {"trait": "lockable", "action": "unlock", "confirm_pin": "1234"},
                                  confirm_verifier=verifier)
    assert code == 200 and body["reported"] == {"locked": False}, body


def test_sensitive_with_bad_pin_denied():
    verifier = lambda dev, pin: pin == "1234"
    code, body = C.handle_command(_issuer(), "lock_front",
                                  {"trait": "lockable", "action": "unlock", "confirm_pin": "0000"},
                                  confirm_verifier=verifier)
    assert code == 403 and body["reason"] == "confirm-required", body


def test_sha_confirm_token_gates_unlock():
    # the real software second factor: confirm_pin must equal SHA256("ha-confirm:"+master)
    from server.control import secret_store as S
    v = S.make_confirm_verifier("CHANGE_ME_master_passphrase")
    tok = S.confirm_token("CHANGE_ME_master_passphrase")
    ok, _ = C.handle_command(_issuer(), "lock_front",
                             {"trait": "lockable", "action": "unlock", "confirm_pin": tok},
                             confirm_verifier=v)
    assert ok == 200, ok
    bad, body = C.handle_command(_issuer(), "lock_front",
                                 {"trait": "lockable", "action": "unlock", "confirm_pin": "CHANGE_ME_master_passphrase"},
                                 confirm_verifier=v)   # the MASTER itself is NOT the token → denied
    assert bad == 403 and body["reason"] == "confirm-required", (bad, body)


def test_router_requires_admin_bearer():
    """End-to-end auth at the router: no/!wrong bearer → 401; correct SHA-derived bearer → reaches the
    PEP. Skips cleanly if fastapi isn't installed (keeps the suite dependency-light)."""
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except Exception:
        print("    (skip: fastapi/testclient not available)")
        return
    from server.control import secret_store as S
    master = "CHANGE_ME_master_passphrase"
    app = FastAPI()
    app.include_router(C.make_router(_issuer(), S.make_confirm_verifier(master),
                                     S.make_api_token_verifier(master)))
    client = TestClient(app)
    body = {"trait": "switchable", "action": "set", "args": {"on": True}}

    # no bearer → 401
    assert client.post("/devices/lamp_office/command", json=body).status_code == 401
    # wrong bearer → 401
    assert client.post("/devices/lamp_office/command", json=body,
                       headers={"Authorization": "Bearer nope"}).status_code == 401
    # the master itself is NOT the bearer → 401
    assert client.post("/devices/lamp_office/command", json=body,
                       headers={"Authorization": f"Bearer {master}"}).status_code == 401
    # correct SHA-derived bearer → reaches the PEP and switches the lamp
    r = client.post("/devices/lamp_office/command", json=body,
                    headers={"Authorization": f"Bearer {S.api_token(master)}"})
    assert r.status_code == 200 and r.json()["status"] == "ok", (r.status_code, r.text)


# ── house scene API (Home/Away/Sleep) ────────────────────────────────────────────
def _ctrl_db():
    import sqlite3
    from server.control import control_store as store
    c = sqlite3.connect(":memory:")
    store.ensure_schema(c)
    store.seed_policy(c, "dehumidifier_office",
                      {"enabled": True, "control": {"on_above": 44, "off_below": 40}})
    return c


def test_set_scene_accepts_canonical_and_rejects_unknown():
    c = _ctrl_db()
    code, body = C.handle_set_scene(c, {"scene": "Away"})
    assert code == 200 and body["scene"] == "Away" and "Home" in body["scenes"], body
    code, body = C.handle_set_scene(c, {"scene": "Vacation"})
    assert code == 400, body


def test_policy_update_accepts_valid_scenes_map():
    c = _ctrl_db()
    patch = {"scenes": {"Away": {"on_above": 60, "off_below": 55}, "Sleep": {"off": True}}}
    code, body = C.handle_policy_update(c, "dehumidifier_office", patch, {"dehumidifier_office"})
    assert code == 200 and body["policy"]["scenes"]["Sleep"]["off"] is True, body


def test_policy_update_rejects_bad_scene_name_and_deadband():
    c = _ctrl_db()
    code, _ = C.handle_policy_update(c, "dehumidifier_office",
                                     {"scenes": {"Nope": {"off": True}}}, {"dehumidifier_office"})
    assert code == 400
    code, _ = C.handle_policy_update(c, "dehumidifier_office",
                                     {"scenes": {"Away": {"on_above": 50, "off_below": 55}}},
                                     {"dehumidifier_office"})
    assert code == 400          # on_above must exceed off_below
    code, _ = C.handle_policy_update(c, "dehumidifier_office",
                                     {"scenes": {"Away": {"bogus": 1}}}, {"dehumidifier_office"})
    assert code == 400          # unknown key


def test_read_control_state_surfaces_active_scene():
    c = _ctrl_db()
    C.handle_policy_update(c, "dehumidifier_office",
                           {"scenes": {"Sleep": {"off": True}}}, {"dehumidifier_office"})
    C.handle_set_scene(c, {"scene": "Sleep"})
    import time
    st = C.read_control_state(c, "dehumidifier_office", time.time())
    assert st["scene"] == "Sleep"
    assert st["scene_active"]["off"] is True and st["scene_active"]["scene"] == "Sleep", st


if __name__ == "__main__":
    run_module(globals())
