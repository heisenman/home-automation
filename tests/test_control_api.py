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


if __name__ == "__main__":
    run_module(globals())
