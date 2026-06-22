"""Tests for the Aranet manufacturer-0x0702 decoder.

The expected values were cross-checked byte-for-byte against the reference `aranet4` library on the
live AranetRn+ (temp 18.4, humidity 60.2, pressure 1006.6, battery 92, radon 9, status green).
"""
from server.ingest.decoders import aranet as A
from tests._harness import run_module

# captured live from AA:BB:CC:00:00:05 (manufacturer 0x0702, 24 bytes)
RAW = bytes.fromhex("0321000c010000000900700152275a02005c0158024d0249")


def test_is_aranet():
    assert A.is_aranet({0x0702: RAW}) is True
    assert A.is_aranet({0x0969: b"x"}) is False
    assert A.is_aranet({}) is False


def test_decode_matches_reference_lib():
    out = A.decode_manufacturer("AA:BB:CC:00:00:05", {0x0702: RAW}, -58)
    assert out is not None
    assert out["device_type"] == "aranet_radon_plus"
    m = out["metrics"]
    assert m["temperature_c"] == 18.4, m
    assert m["humidity_pct"] == 60.2, m
    assert m["pressure_hpa"] == 1006.6, m
    assert m["battery_pct"] == 92, m
    assert m["radon_bqm3"] == 9, m
    assert out["meta"]["status"] == 1 and out["meta"]["interval_s"] == 600, out["meta"]
    assert out["meta"]["ago_s"] == 589, out["meta"]


def test_radon_warming_up_omitted():
    raw = bytearray(RAW)
    raw[8], raw[9] = 0xFF, 0xFF        # 0xFFFF = no radon reading yet
    out = A.decode_manufacturer("x", {0x0702: bytes(raw)}, -60)
    assert "radon_bqm3" not in out["metrics"]
    assert out["metrics"]["temperature_c"] == 18.4   # other metrics still decode


def test_too_short_and_wrong_type():
    assert A.decode_manufacturer("x", {0x0702: b"\x03\x21\x00"}, -60) is None
    bad = bytearray(RAW); bad[0] = 0x01      # not a radon device
    assert A.decode_manufacturer("x", {0x0702: bytes(bad)}, -60) is None


if __name__ == "__main__":
    run_module(globals())
