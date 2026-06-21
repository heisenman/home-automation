"""End-to-end control loop: PEP (issuer) -> Loopback -> SimActuator (verify+apply+ack).

Proves the secured command path with no hardware: genuine commands flow and reconcile; the device
refuses a forged command even if the PEP itself is wrong; policy denies (range, confirm, rate-limit,
mode) stop before anything is sent.
"""
from server.control import protocol
from server.control.issuer import CommandIssuer, DeviceCtl, LoopbackTransport
from server.control.policy import PolicyStore
from server.control.sim import SimActuator
from tests._harness import run_module

T0 = 1_780_000_000.0
SECRETS = {"lock_front": "secret-lock-AAA", "lamp_office": "secret-lamp-BBB"}

REGISTRY = {
    "lock_front": DeviceCtl("lock_front", node="c6-bench", area="entry", traits_cfg={"lockable": {}}),
    "lamp_office": DeviceCtl("lamp_office", node="c6-bench", area="c_office",
                             traits_cfg={"switchable": {}, "ranged": {"min": 0, "max": 100}}),
}
POLICY = PolicyStore({
    "version": 1,
    "guardrails": {"lamp_office": {"ranged": {"rate_limit_s": 10}}},
    "modes": {"Emergency": {"deny": [{"trait": "lockable", "action": "unlock"}], "force_safe": True}},
})


def _fresh_env(mode="Normal"):
    sims = {d: SimActuator(d, REGISTRY[d].traits_cfg, SECRETS[d]) for d in REGISTRY}
    state = {"mode": mode}
    issuer = CommandIssuer(registry=REGISTRY, secrets=SECRETS, policy=POLICY,
                           transport=LoopbackTransport(sims), mode_getter=lambda: state["mode"])
    return issuer, sims, state


def test_switch_on_reconciles():
    issuer, sims, _ = _fresh_env()
    r = issuer.issue(device_id="lamp_office", trait="switchable", action="set",
                     args={"on": True}, now=T0)
    assert r.status == "ok" and r.reported == {"on": True, "level": 0}, (r.status, r.reported)
    assert sims["lamp_office"].state["on"] is True


def test_out_of_range_rejected_before_send():
    issuer, sims, _ = _fresh_env()
    r = issuer.issue(device_id="lamp_office", trait="ranged", action="set",
                     args={"level": 150}, now=T0)
    assert r.status == "rejected" and r.reason.startswith("contract:"), (r.status, r.reason)
    assert sims["lamp_office"].state["level"] == 0    # unchanged — nothing was sent


def test_unlock_requires_confirm_then_succeeds():
    issuer, sims, _ = _fresh_env()
    r1 = issuer.issue(device_id="lock_front", trait="lockable", action="unlock", now=T0)
    assert r1.status == "rejected" and r1.reason == "confirm-required", (r1.status, r1.reason)
    assert sims["lock_front"].state["locked"] is True
    r2 = issuer.issue(device_id="lock_front", trait="lockable", action="unlock",
                      confirmed=True, now=T0 + 1)
    assert r2.status == "ok" and r2.reported == {"locked": False}, (r2.status, r2.reported)


def test_lock_is_not_sensitive():
    issuer, _, _ = _fresh_env()
    r = issuer.issue(device_id="lock_front", trait="lockable", action="lock", now=T0)
    assert r.status == "ok" and r.reported == {"locked": True}, (r.status, r.reported)


def test_rate_limit():
    issuer, _, _ = _fresh_env()
    a = issuer.issue(device_id="lamp_office", trait="ranged", action="set", args={"level": 40}, now=T0)
    b = issuer.issue(device_id="lamp_office", trait="ranged", action="set", args={"level": 60}, now=T0 + 3)
    c = issuer.issue(device_id="lamp_office", trait="ranged", action="set", args={"level": 60}, now=T0 + 20)
    assert a.status == "ok" and b.reason == "rate-limited" and c.status == "ok", (a.status, b.reason, c.status)


def test_emergency_mode_denies_unlock():
    issuer, _, state = _fresh_env(mode="Emergency")
    r = issuer.issue(device_id="lock_front", trait="lockable", action="unlock",
                     confirmed=True, now=T0)
    assert r.status == "rejected" and r.reason == "mode-Emergency-denies", (r.status, r.reason)


def test_forged_command_bounces_at_device():
    # The PEP is given the WRONG secret for the device → the device refuses despite a valid-looking PEP.
    sims = {"lamp_office": SimActuator("lamp_office", REGISTRY["lamp_office"].traits_cfg,
                                       SECRETS["lamp_office"])}
    rogue = CommandIssuer(registry={"lamp_office": REGISTRY["lamp_office"]},
                          secrets={"lamp_office": "WRONG-SECRET"}, policy=POLICY,
                          transport=LoopbackTransport(sims))
    r = rogue.issue(device_id="lamp_office", trait="switchable", action="set",
                    args={"on": True}, now=T0)
    assert r.status == "rejected" and r.reason == "bad-sig", (r.status, r.reason)
    assert sims["lamp_office"].state["on"] is False    # device did not act


def test_unknown_device():
    issuer, _, _ = _fresh_env()
    r = issuer.issue(device_id="ghost", trait="switchable", action="set", args={"on": True}, now=T0)
    assert r.status == "unknown-device"


def test_standing_orders_emergency_locks():
    orders = POLICY.standing_orders(device_id="lock_front",
                                    traits_cfg=REGISTRY["lock_front"].traits_cfg, mode="Emergency")
    assert orders == {"locked": True}


if __name__ == "__main__":
    run_module(globals())
