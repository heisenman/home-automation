"""ADR-0023 mesh reach census — mapper ingest (home/edge/<node>/reach) + coordinator push trigger."""
import hashlib
import hmac
import json
import sqlite3

from server.ingest.edge_mapper import EdgeMapper
from server.mesh import store as mesh_store
from server.mesh.coordinator import sign_envelope, trigger_reach

MAC_A = "AA:BB:CC:00:00:01"
MAC_B = "AA:BB:CC:00:00:02"
REG = {
    MAC_A: {"device_id": "m1", "area": "office", "device_type": "switchbot_meter"},
    MAC_B: {"device_id": "m2", "area": "kitchen", "device_type": "switchbot_meter"},
}


class _Msg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = json.dumps(payload).encode()


class _Client:
    def __init__(self):
        self.pubs = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.pubs.append((topic, json.loads(payload), qos, retain))


def _mapper_with_memdb():
    """EdgeMapper wired to an in-memory mesh so we can assert on mesh_links without touching instance/."""
    m = EdgeMapper(REG, _Client())
    m._mesh = sqlite3.connect(":memory:")
    mesh_store.ensure_schema(m._mesh)
    return m


def _links(conn):
    return {(r[1], r[3]): r[5] for r in conn.execute(
        "SELECT src_kind,src_id,dst_kind,dst_id,link_kind,rssi FROM mesh_links")}


# ── mapper: a reach census records passive sightings for the WHOLE neighborhood ────────────────────
def test_reach_census_records_known_endpoints_as_passive_sightings():
    m = _mapper_with_memdb()
    msg = _Msg("home/edge/s3-crawlspace/reach", {
        "schema": 1, "node": "s3-crawlspace", "ts": "2026-07-02T00:00:00Z",
        "reach": [
            {"mac": MAC_A, "rssi_ewma": -71, "count": 40, "age_s": 2},
            {"mac": MAC_B, "rssi_ewma": -88, "count": 5, "age_s": 9},
        ],
    })
    m.on_message(m._mqtt, None, msg)
    links = _links(m._mesh)
    assert links[("s3-crawlspace", "m1")] == -71     # node→endpoint reach, smoothed rssi persisted
    assert links[("s3-crawlspace", "m2")] == -88
    # a reach census is metadata only — it must NOT republish a canonical reading
    assert m._mqtt.pubs == []


def test_reach_census_drops_unknown_macs():
    m = _mapper_with_memdb()
    msg = _Msg("home/edge/c6-bench/reach", {
        "node": "c6-bench",
        "reach": [
            {"mac": MAC_A, "rssi_ewma": -60, "count": 30, "age_s": 1},
            {"mac": "DE:AD:BE:EF:00:00", "rssi_ewma": -50, "count": 99, "age_s": 0},  # not in registry
        ],
    })
    m.on_message(m._mqtt, None, msg)
    links = _links(m._mesh)
    assert ("c6-bench", "m1") in links
    assert len(links) == 1     # the unknown MAC is dropped, exactly like the advert path


def test_reach_census_bad_payload_never_raises():
    m = _mapper_with_memdb()
    m.on_message(m._mqtt, None, _Msg("home/edge/x/reach", {"node": "x"}))          # missing reach[]
    m.on_message(m._mqtt, None, _Msg("home/edge/x/reach", {"node": "x", "reach": "nope"}))
    assert _links(m._mesh) == {}   # nothing recorded, no exception


# ── coordinator: the server-push trigger is signed, sig-only, per enrolled node ────────────────────
def test_trigger_reach_signs_and_publishes_per_enrolled_node():
    lut = {"s3": {"cmd_secret": "k1"}, "c6": {"cmd_secret": "k2"}, "unprov": {}}
    client = _Client()
    n = trigger_reach(client, lut)
    assert n == 2                                        # only the two enrolled nodes
    by_topic = {t: (env, qos, retain) for t, env, qos, retain in client.pubs}
    assert set(by_topic) == {"home/edge/s3/reach/req", "home/edge/c6/reach/req"}
    env, qos, retain = by_topic["home/edge/s3/reach/req"]
    assert retain is False                               # a trigger is transient, never retained
    # firmware verifies HMAC over the literal p string (cmd_sig_ok); check s3's secret signs it
    assert hmac.compare_digest(env["s"], hmac.new(b"k1", env["p"].encode(), hashlib.sha256).hexdigest())
    assert json.loads(env["p"]) == {"op": "reach"}


def test_trigger_reach_noop_without_client():
    assert trigger_reach(None, {"s3": {"cmd_secret": "k1"}}) == 0


def test_trigger_reach_envelope_matches_firmware_relay_path():
    # sig-only: identical to how the firmware's handle_reach_req verifies (same cmd_sig_ok as handle_relay)
    env = sign_envelope("sek", {"op": "reach"})
    assert hmac.compare_digest(env["s"], hmac.new(b"sek", env["p"].encode(), hashlib.sha256).hexdigest())
