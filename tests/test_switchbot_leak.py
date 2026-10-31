"""SwitchBot Water Leak Detector decode + edge-mapper handling. Frames are the bench unit's real adverts
(2026-10-31) with the MAC prefix replaced by a placeholder."""
import json

from server.ingest.decoders import switchbot
from server.ingest.edge_mapper import EdgeMapper

MFR_ID = switchbot.MANUFACTURER_ID
PFX = bytes.fromhex("aabbcc000001")
DRY = PFX + bytes.fromhex("0c2998000001" "6c3c0500000000")
WET = PFX + bytes.fromhex("0e29f9000002" "743c0500000000")
SVC = {switchbot.SERVICE_UUID: bytes.fromhex("260029")}
MAC = "AA:BB:CC:00:00:01"


def test_passive_advert_decodes_as_leak_by_registry_type():
    r = switchbot.decode(MAC, {MFR_ID: WET}, {}, -60, device_type_hint="switchbot_leak")
    assert r == {"device_type": "switchbot_leak",
                 "metrics": {"leak": 1, "tampered": 0, "battery_pct": 41, "leak_event_count": 2}}
    r = switchbot.decode(MAC, {MFR_ID: DRY}, {}, -60, device_type_hint="switchbot_leak")
    assert r["metrics"]["leak"] == 0 and r["metrics"]["leak_event_count"] == 1


def test_scan_response_model_byte_identifies_the_detector_without_a_hint():
    r = switchbot.decode(MAC, {MFR_ID: WET}, SVC, -60)
    assert r["device_type"] == "switchbot_leak" and r["metrics"]["leak"] == 1


def test_without_a_hint_a_passive_leak_advert_is_still_a_meter_guess():
    # what the edge firmware does today: -0.9 C / 0 % — the reason the mapper must drop it
    r = switchbot.decode(MAC, {MFR_ID: WET}, {}, -60)
    assert r["device_type"] == "switchbot_meter" and r["metrics"]["temperature_c"] == -0.9


class _Client:
    def __init__(self):
        self.pubs = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.pubs.append((topic, json.loads(payload)))


def _msg(node, metrics, rssi):
    class M:
        topic = f"home/edge/{node}/{MAC}/adv"
        payload = json.dumps({"schema": 1, "node": node, "mac": MAC, "metrics": metrics,
                              "meta": {"rssi": rssi}}).encode()
    return M()


def test_mapper_drops_edge_meter_misdecode_and_keeps_local_leak_frames():
    reg = {MAC: {"device_id": "leak_kitchen_sink", "area": "kitchen", "device_type": "switchbot_leak"}}
    c = _Client()
    m = EdgeMapper(reg, c, relay_dedup=True)
    m.on_message(c, None, _msg("hoffice_c6", {"temperature_c": -0.9, "humidity_pct": 0}, -40))  # strong but bogus
    m.on_message(c, None, _msg("local", {"leak": 1, "tampered": 0, "battery_pct": 41}, -65))
    canon = [p for t, p in c.pubs if t == "home/kitchen/leak_kitchen_sink/state"]
    assert [p["metrics"].get("leak") for p in canon] == [1]
