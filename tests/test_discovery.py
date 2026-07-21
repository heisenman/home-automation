"""Tests for the BLE 'Add sensor' discovery cache (server/ingest/discovery.py) — the server half of the
PWA onboarding flow that surfaces unregistered SwitchBot adverts from home/unknown/+/raw."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.ingest.discovery import DiscoveryCache, decode_raw


def _outdoor_raw(mac="D8:BF:C4:C6:28:31", rssi=-42, temp_c=22.5, hum=45, batt=100):
    # Format-B (manufacturer 0x0969) outdoor/newer-Pro advert: [6-byte MAC][status][seq][tfrac][tint|0x80][hum]
    frac = int(round((temp_c - int(temp_c)) * 10)) & 0x0F
    tint = (int(temp_c) & 0x7F) | 0x80
    mfr = bytes.fromhex("aabbccddeeff") + bytes([0x00, 0x00, frac, tint, hum & 0x7F])
    svc = bytes([0x77, 0x00, batt & 0x7F])   # model 'w' (outdoor) + battery in byte 2
    return {"brand": "switchbot", "mac": mac, "rssi": rssi,
            "manufacturer_data": {"2409": mfr.hex()},
            "service_data": {"0000fd3d-0000-1000-8000-00805f9b34fb": svc.hex()}}


def test_decode_raw_outdoor_format_b():
    d = decode_raw(_outdoor_raw())
    assert d == {"device_type": "switchbot_meter_outdoor",
                 "temperature_c": 22.5, "humidity_pct": 45, "battery_pct": 100}


def test_decode_raw_rejects_non_switchbot_and_garbage():
    assert decode_raw({"brand": "aranet", "mac": "AA"}) is None
    assert decode_raw({"brand": "switchbot", "mac": "AA", "manufacturer_data": {"2409": "zzzz"}}) is None
    assert decode_raw({}) is None
    assert decode_raw(None) is None


def test_cache_ranks_by_signal_and_adds_fahrenheit():
    c = DiscoveryCache()
    c.ingest(_outdoor_raw(mac="AA:AA:AA:AA:AA:AA", rssi=-80), now=100.0)
    c.ingest(_outdoor_raw(mac="D8:BF:C4:C6:28:31", rssi=-55), now=100.0)
    c.ingest(_outdoor_raw(mac="D8:BF:C4:C6:28:31", rssi=-34), now=101.0)   # got closer
    cands = c.candidates({}, now=102.0)
    assert [r["mac"] for r in cands] == ["D8:BF:C4:C6:28:31", "AA:AA:AA:AA:AA:AA"]  # strongest first
    top = cands[0]
    assert top["rssi_max"] == -34 and top["count"] == 2
    assert top["temperature_f"] == 72.5 and top["age_s"] == 1.0


def test_cache_filters_registered_macs():
    c = DiscoveryCache()
    c.ingest(_outdoor_raw(mac="D8:BF:C4:C6:28:31"), now=100.0)
    assert c.candidates({"D8:BF:C4:C6:28:31": {"device_id": "meter_x"}}, now=100.0) == []
    # case-insensitive match on the registry key: lowercase-registered ...32 is filtered, ...31 remains
    c.ingest(_outdoor_raw(mac="D8:BF:C4:C6:28:32"), now=100.0)
    remaining = [r["mac"] for r in c.candidates({"d8:bf:c4:c6:28:32": {}}, now=100.0)]
    assert remaining == ["D8:BF:C4:C6:28:31"]


def test_cache_evicts_expired():
    c = DiscoveryCache(ttl_s=300)
    c.ingest(_outdoor_raw(), now=100.0)
    assert len(c.candidates({}, now=200.0)) == 1        # within TTL
    assert c.candidates({}, now=100.0 + 301) == []       # aged out
