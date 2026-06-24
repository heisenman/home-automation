"""ADR-0015 Phase B — coordinator allowlist computation + signing/debounce/publish."""
import hashlib
import hmac
import json
import sqlite3

from server.mesh import store as mesh_store
from server.mesh.coordinator import (DEFAULT_DWELL_S, build_directive, compute_allowlists, publish_pass,
                                     reconcile, sign_envelope)
from server.mesh.topology import SERVER, Link


def test_edge_only_device_goes_to_that_node_local_device_stays_local():
    links = [
        # device X: only c6 hears it -> c6 is the preferred source
        Link(SERVER, ("node", "c6"), "ip"),
        Link(("node", "c6"), ("endpoint", "X"), "ble-adv", rssi=-60),
        # device Y: dictator's own radio hears it strongly -> local preferred (no edge directive)
        Link(SERVER, ("endpoint", "Y"), "ble-adv", rssi=-55),
    ]
    per_node, local_devs = compute_allowlists(links, {"X": "AA:AA", "Y": "BB:BB"})
    assert local_devs == ["Y"]
    assert per_node["c6"]["device_ids"] == ["X"]
    assert per_node["c6"]["relay_macs"] == ["AA:AA"]


def test_local_wins_ties_so_edge_allowlist_empty():
    # both hear X at equal rssi -> local wins (saves the IP hop) -> no edge directive at all.
    links = [
        Link(SERVER, ("endpoint", "X"), "ble-adv", rssi=-70),
        Link(SERVER, ("node", "c6"), "ip"),
        Link(("node", "c6"), ("endpoint", "X"), "ble-adv", rssi=-70),
    ]
    per_node, local_devs = compute_allowlists(links, {"X": "AA:AA"})
    assert local_devs == ["X"] and per_node == {}


def test_coordinator_injects_backhaul_so_a_fresh_edge_wins():
    # mesh.db-style input: ONLY receiver->endpoint reach edges (no SERVER->node ip backhaul). Local heard
    # X long ago (stale), s3 is hearing it now. The coordinator must inject the backhaul AND the recency
    # cost must then pick s3. (Regression: without the inject, s3 is unreachable -> X wrongly goes local.)
    links = [
        Link(SERVER, ("endpoint", "X"), "ble-adv", rssi=-60, age_s=800),
        Link(("node", "s3"), ("endpoint", "X"), "ble-adv", rssi=-75, age_s=10),
    ]
    per_node, local_devs = compute_allowlists(links, {"X": "AA:AA"})
    assert "X" not in local_devs
    assert per_node["s3"]["device_ids"] == ["X"]


def test_build_directive_shape():
    d = build_directive(["BB:BB", "AA:AA"], 5)
    assert d["type"] == "relay_assign" and d["epoch"] == 5
    assert d["relay_macs"] == ["AA:AA", "BB:BB"] and d["cmd_relay"] == []


# ── signing: the firmware {p,s} envelope ──────────────────────────────────────
def test_sign_envelope_matches_firmware_hmac_over_literal_p():
    secret = "node-secret-123"
    payload = build_directive(["AA:BB:CC:DD:EE:FF"], 3)
    env = sign_envelope(secret, payload)
    # firmware verifies HMAC over the LITERAL p string it receives (cmd_sig_ok), then parses p as JSON
    expected = hmac.new(secret.encode(), env["p"].encode(), hashlib.sha256).hexdigest()
    assert hmac.compare_digest(env["s"], expected)
    assert json.loads(env["p"])["relay_macs"] == ["AA:BB:CC:DD:EE:FF"]
    # deterministic: same payload signs identically (so unchanged sets don't look "changed")
    assert sign_envelope(secret, build_directive(["AA:BB:CC:DD:EE:FF"], 3)) == env


# ── reconcile: debounce + monotonic epoch (decision #5) ───────────────────────
def test_reconcile_cold_start_dwells_then_publishes():
    act, st = reconcile(None, ["AA"], now=0.0, dwell_s=900)
    assert act == "pending" and st["epoch"] == 0 and st["pending_macs"] == ["AA"]
    act, st2 = reconcile(st, ["AA"], now=500, dwell_s=900)          # mid-dwell
    assert act == "noop"
    act, st3 = reconcile(st, ["AA"], now=900, dwell_s=900)          # dwell met
    assert act == "publish" and st3["epoch"] == 1 and st3["relay_macs"] == ["AA"] and st3["pending_macs"] is None


def test_reconcile_steady_is_noop():
    published = {"epoch": 2, "relay_macs": ["AA", "BB"], "pending_macs": None, "pending_since": None}
    assert reconcile(published, ["BB", "AA"], now=10, dwell_s=900)[0] == "noop"


def test_reconcile_flap_restarts_dwell_no_publish():
    published = {"epoch": 1, "relay_macs": ["AA"], "pending_macs": None, "pending_since": None}
    act, st = reconcile(published, ["BB"], now=0, dwell_s=900)      # candidate BB
    assert act == "pending" and st["epoch"] == 1
    act2, st2 = reconcile(st, ["CC"], now=100, dwell_s=900)         # flips to CC before dwell -> restart
    assert act2 == "pending" and st2["pending_macs"] == ["CC"] and st2["pending_since"] == 100


def test_reconcile_drop_to_empty_publishes_empty_allowlist():
    published = {"epoch": 4, "relay_macs": ["AA"], "pending_macs": None, "pending_since": None}
    act, st = reconcile(published, [], now=0, dwell_s=0)            # node no longer preferred for anything
    assert act == "publish" and st["epoch"] == 5 and st["relay_macs"] == []


def test_relay_state_store_roundtrip():
    c = sqlite3.connect(":memory:")
    mesh_store.ensure_schema(c)
    mesh_store.save_relay_state(c, "s3", 3, ["BB", "AA"], ["CC"], 123.0)
    st = mesh_store.load_relay_state(c)["s3"]
    assert st["epoch"] == 3 and st["relay_macs"] == ["AA", "BB"]
    assert st["pending_macs"] == ["CC"] and st["pending_since"] == 123.0


class _FakeClient:
    def __init__(self): self.pubs = []
    def publish(self, topic, payload, qos=0, retain=False):
        self.pubs.append((topic, json.loads(payload), qos, retain))


def test_publish_pass_signs_publishes_retained_then_idempotent():
    c = sqlite3.connect(":memory:")
    mesh_store.ensure_schema(c)
    # c6 is the only receiver of device X -> it should be the preferred source -> allowlist {X's MAC}
    mesh_store.record_link(c, ("node", "c6"), ("endpoint", "X"), "ble-adv", rssi=-60, ok=None)
    lut = {"c6": {"cmd_secret": "sek"}}
    client = _FakeClient()
    out = publish_pass(c, {"X": "AA:AA"}, lut, client=client, now=1000.0, dwell_s=0)
    assert out["published"] == ["c6"] and len(client.pubs) == 1
    topic, env, qos, retain = client.pubs[0]
    assert topic == "home/edge/c6/relay" and qos == 1 and retain is True
    assert hmac.compare_digest(
        env["s"], hmac.new(b"sek", env["p"].encode(), hashlib.sha256).hexdigest())
    assert json.loads(env["p"])["relay_macs"] == ["AA:AA"] and json.loads(env["p"])["epoch"] == 1
    # second identical pass -> nothing new published (steady)
    out2 = publish_pass(c, {"X": "AA:AA"}, lut, client=client, now=1001.0, dwell_s=0)
    assert out2["published"] == [] and len(client.pubs) == 1


def test_publish_pass_skips_unprovisioned_node():
    c = sqlite3.connect(":memory:")
    mesh_store.ensure_schema(c)
    mesh_store.record_link(c, ("node", "c6"), ("endpoint", "X"), "ble-adv", rssi=-60, ok=None)
    out = publish_pass(c, {"X": "AA:AA"}, lut={}, client=_FakeClient(), now=1000.0, dwell_s=0)
    assert out["skipped_no_secret"] == ["c6"] and out["published"] == []
