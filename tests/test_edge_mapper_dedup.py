"""ADR-0015 Phase A — edge_mapper preferred-source gate (integration, fake MQTT client)."""
import json

from server.ingest.edge_mapper import EdgeMapper

MAC = "AA:BB:CC:00:00:01"
REG = {MAC: {"device_id": "m1", "area": "office", "device_type": "switchbot_meter"}}


class _Msg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = json.dumps(payload).encode()


class _Client:
    def __init__(self):
        self.pubs = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.pubs.append((topic, json.loads(payload)))


def _adv(node, rssi):
    return _Msg(f"home/edge/{node}/{MAC}/adv",
                {"schema": 1, "node": node, "mac": MAC,
                 "metrics": {"temperature_c": 21}, "meta": {"rssi": rssi}})


def _canonical(client):
    return [p for p in client.pubs if p[0] == "home/office/m1/state"]


def test_dedup_on_publishes_only_preferred_source():
    c = _Client()
    m = EdgeMapper(REG, c, relay_dedup=True)
    m.on_message(c, None, _adv("local", -50))   # strong local → becomes preferred → publishes
    m.on_message(c, None, _adv("c6", -70))       # weaker edge, non-preferred → dropped
    canon = _canonical(c)
    assert len(canon) == 1, f"expected 1 canonical write, got {len(canon)}"
    assert canon[0][1]["meta"]["node"] == "local"


def test_dedup_off_publishes_every_source():
    c = _Client()
    m = EdgeMapper(REG, c, relay_dedup=False)
    m.on_message(c, None, _adv("local", -50))
    m.on_message(c, None, _adv("c6", -70))
    assert len(_canonical(c)) == 2  # today's behaviour: every source republishes (writer dedups)
