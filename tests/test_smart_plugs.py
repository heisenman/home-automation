"""Smart plugs as switches (Hugh, 2026-11-05): remote on/off for every plug, a PLUG-timed power-cycle, and a
per-plug power mode — computers' plugs default to power-cycle only (no plain Off), the rest on/off."""
import sqlite3

import pytest

from server.api import control as C
from server.api.viewmodel import build_controls
from server.control import control_store as store
from server.control import plug_driver as P
from server.control import plug_power as PP
from server.control import protocol, traits
from server.control.issuer import CommandIssuer, DeviceCtl
from server.control.policy import PolicyStore

SW = {"switchable": {"power_mode": "on_off", "cycle": {"max_s": 120}, "now_metric": "relay_on"}}
SW_CYC = {"switchable": {"power_mode": "cycle_only", "cycle": {"max_s": 120}, "now_metric": "relay_on"}}
REGISTRY = {
    "plug_hall": DeviceCtl("plug_hall", "plug_01", "hall", SW, device_type="plug"),
    "dehum_pm": DeviceCtl("dehum_pm", "server", "attic", SW, device_type="tasmota_plug"),
    "failover_pm": DeviceCtl("failover_pm", "server", "h_office", SW_CYC, device_type="tasmota_plug"),
    "lamp": DeviceCtl("lamp", "c6-bench", "c_office", {"switchable": {}}),
}
MASTER = "CHANGE_ME_master_passphrase"


# ── trait: switchable/cycle ──────────────────────────────────────────────────────────────────────────────

def test_cycle_validates_off_s_against_the_cfg():
    t = traits.get_trait("switchable")
    assert t.validate_command("cycle", {"off_s": 30}, SW["switchable"]) == {"off_s": 30}
    with pytest.raises(traits.TraitError):
        t.validate_command("cycle", {"off_s": 500}, SW["switchable"])
    with pytest.raises(traits.TraitError):          # a switch with no declared cycle can't be cycled
        t.validate_command("cycle", {"off_s": 10}, {})


# ── transports: routing by type + wire messages ──────────────────────────────────────────────────────────

def test_plugs_are_routed_by_type():
    assert P.plug_devices_of(REGISTRY) == {"plug_hall": "plug_01"}
    assert P.tasmota_plugs_of(REGISTRY) == {"dehum_pm": "dehum_pm", "failover_pm": "failover_pm"}


def test_tasmota_cycle_is_timed_by_the_plug():
    assert P.TasmotaPlugTransport.message("failover_pm", "cycle", 30) == \
        ("cmnd/failover_pm/Backlog", "Power 0; Delay 300; Power 1", False)
    assert P.TasmotaPlugTransport.message("dehum_pm", "set", True) == ("cmnd/dehum_pm/POWER", "ON", True)


def test_pg03_set_and_cycle_sign_the_node_ops(monkeypatch):
    sent = []

    def fake_send(mqtt, broker, port, node, secret, inner, topic, matches, wait_s, state_dir, device_id=""):
        sent.append((node, inner, topic))
        m = {"relay_on": inner.get("on", False)}
        return [m] if matches(m) else []
    monkeypatch.setattr(P, "send_and_confirm", fake_send)
    t = P.PlugEdgeTransport.__new__(P.PlugEdgeTransport)
    t._mqtt, t.devices, t.lut = None, {"plug_hall": "plug_01"}, {"plug_01": {"cmd_secret": "k"}}
    t.broker, t.port, t.state_dir, t.confirm_s = "x", 1, None, 0
    on = t.send_and_wait(node="plug_01", device_id="plug_hall", area="hall",
                         cmd={"id": "1", "trait": "switchable", "action": "set", "args": {"on": True}})
    cy = t.send_and_wait(node="plug_01", device_id="plug_hall", area="hall",
                         cmd={"id": "2", "trait": "switchable", "action": "cycle", "args": {"off_s": 10}})
    assert sent[0] == ("plug_01", {"op": "plug_set", "on": True}, "home/edge/plug_01/plug/adv")
    assert sent[1][1] == {"op": "plug_cycle", "off_s": 10}
    assert on["status"] == "ok" and on["reported_state"] == {"on": True}
    assert cy["status"] == "ok" and cy["reported_state"] == {"on": False, "off_s": 10}


def test_pg03_without_a_secret_is_rejected_not_sent():
    t = P.PlugEdgeTransport.__new__(P.PlugEdgeTransport)
    t.devices, t.lut = {"plug_hall": "plug_01"}, {}
    a = t.send_and_wait(node="plug_01", device_id="plug_hall", area="hall",
                        cmd={"id": "1", "trait": "switchable", "action": "set", "args": {"on": True}})
    assert a["status"] == "rejected" and "cmd_secret" in a["reason"]


# ── power mode ───────────────────────────────────────────────────────────────────────────────────────────

def test_mode_defaults_from_registry_and_is_selectable():
    conn = sqlite3.connect(":memory:")
    store.ensure_schema(conn)
    assert PP.get_mode(conn, REGISTRY, "failover_pm") == "cycle_only"
    assert PP.get_mode(conn, REGISTRY, "dehum_pm") == "on_off"
    assert PP.get_mode(conn, REGISTRY, "lamp") is None                  # not a smart plug
    assert PP.handle_set_mode(conn, REGISTRY, "failover_pm", {"mode": "on_off"})[0] == 200
    assert PP.get_mode(conn, REGISTRY, "failover_pm") == "on_off"
    assert PP.handle_set_mode(conn, REGISTRY, "failover_pm", {"mode": "sometimes"})[0] == 400
    assert PP.handle_set_mode(conn, REGISTRY, "lamp", {"mode": "on_off"})[0] == 404


def test_cycle_only_refuses_off_but_not_on_or_cycle():
    assert PP.refusal("cycle_only", "switchable", "set", {"on": False})
    assert PP.refusal("cycle_only", "switchable", "set", {"on": True}) is None
    assert PP.refusal("cycle_only", "switchable", "cycle", {"off_s": 10}) is None
    assert PP.refusal("on_off", "switchable", "set", {"on": False}) is None


# ── PWA controls ─────────────────────────────────────────────────────────────────────────────────────────

def test_controls_carry_cycle_and_mode_and_hide_off_when_cycle_only():
    cs = {c["kind"]: c for c in build_controls(SW_CYC, manual=True, power_mode="cycle_only")}
    assert cs["switchable"]["off_allowed"] is False
    assert cs["cycle"]["action"]["action"] == "cycle" and [o["value"] for o in cs["cycle"]["options"]] == [10, 30, 60]
    assert cs["power_mode"]["now"] == "cycle_only" and cs["power_mode"]["action"]["path"] == "/devices/{id}/power-mode"
    plain = {c["kind"] for c in build_controls({"switchable": {}}, manual=True)}
    assert plain == {"switchable"}                                       # other switches unchanged


# ── API: admin-only, and cycle-only refuses Off before it reaches the plug ───────────────────────────────

class _Tr:
    def __init__(self):
        self.cmds = []

    def send_and_wait(self, *, node, device_id, area, cmd, now=None, timeout=5.0):
        self.cmds.append((device_id, cmd["action"], cmd["args"]))
        rep = {"on": cmd["args"].get("on", False)}
        if cmd["action"] == "cycle":
            rep["off_s"] = cmd["args"]["off_s"]
        return protocol.build_ack(cmd_id=cmd["id"], status="ok", reported_state=rep, source="commanded")


def _client(tmp_path):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from server.control import secret_store as S
    db = tmp_path / "control.db"
    c = sqlite3.connect(db)
    store.ensure_schema(c)
    c.close()
    tr = _Tr()
    iss = CommandIssuer(registry=REGISTRY, secrets={d: "s" for d in REGISTRY}, policy=PolicyStore({"version": 1}),
                        transport=tr)
    admin = S.make_api_token_verifier(MASTER)
    operator = lambda a: a in (f"Bearer {S.api_token(MASTER)}", "Bearer operator")   # noqa: E731
    app = fastapi.FastAPI()
    app.include_router(C.make_router(iss, S.make_confirm_verifier(MASTER), operator, admin_authz=admin,
                                     control_db=db))
    return TestClient(app), tr, {"Authorization": f"Bearer {S.api_token(MASTER)}"}


def test_plug_commands_need_admin_not_operator(tmp_path):
    client, tr, adm = _client(tmp_path)
    on = {"trait": "switchable", "action": "set", "args": {"on": True}}
    assert client.post("/devices/dehum_pm/command", json=on, headers={"Authorization": "Bearer operator"}
                       ).status_code == 401
    assert client.post("/devices/lamp/command", json=on, headers={"Authorization": "Bearer operator"}
                       ).status_code == 200                              # a non-plug switch: operator is fine
    r = client.post("/devices/dehum_pm/command", json=on, headers=adm)
    assert r.status_code == 200 and tr.cmds[-1] == ("dehum_pm", "set", {"on": True})


def test_cycle_only_plug_refuses_off_until_the_mode_changes(tmp_path):
    client, tr, adm = _client(tmp_path)
    off = {"trait": "switchable", "action": "set", "args": {"on": False}}
    r = client.post("/devices/failover_pm/command", json=off, headers=adm)
    assert r.status_code == 409 and "power-cycle only" in r.json()["reason"] and tr.cmds == []
    r = client.post("/devices/failover_pm/command", headers=adm,
                    json={"trait": "switchable", "action": "cycle", "args": {"off_s": 30}})
    assert r.status_code == 200 and tr.cmds[-1] == ("failover_pm", "cycle", {"off_s": 30})
    assert client.put("/devices/failover_pm/power-mode", json={"mode": "on_off"},
                      headers={"Authorization": "Bearer operator"}).status_code == 401
    assert client.put("/devices/failover_pm/power-mode", json={"mode": "on_off"}, headers=adm).status_code == 200
    assert client.post("/devices/failover_pm/command", json=off, headers=adm).status_code == 200
