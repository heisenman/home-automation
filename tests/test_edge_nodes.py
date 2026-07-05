"""Guard for the per-node build/flash manifest (edge/esp32c6/nodes.yaml) and the sensor-aware
secrets.h emit — the ADR-0020 cross-provisioning guard (2026-07-05 cbed_c6<-coffice_c6 mixup).

Ensures: the committed manifest is well-formed, and enroll_node bakes the gas-chip select + identity
into secrets.h so an image can't come up wearing the wrong node id / wrong sensor.
"""
import re
import sys
from pathlib import Path

from tests._harness import raises, run_module

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import struct  # noqa: E402
import tempfile  # noqa: E402

import edge_nodes as EN  # noqa: E402
import enroll_node as ENR  # noqa: E402
import edge_ota as O  # noqa: E402


def _fake_app_bin(version: str) -> str:
    """A minimal file with an esp_app_desc at offset 0x20 (magic + version[32] at desc+16)."""
    desc = struct.pack("<II", 0xABCD5432, 0) + b"\0" * 8 + version.encode().ljust(32, b"\0")
    fd, path = tempfile.mkstemp(suffix=".bin")
    import os
    with os.fdopen(fd, "wb") as f:
        f.write(b"\0" * 0x20 + desc + b"\0" * 64)
    return path


def test_manifest_loads_and_is_wellformed():
    nodes = EN.load()
    assert nodes, "manifest is empty"
    for nid, rec in nodes.items():
        for field in EN.REQUIRED:
            assert rec.get(field), f"{nid}: missing {field}"
        assert rec["sensor"] in EN.SENSORS, f"{nid}: bad sensor {rec['sensor']!r}"
        assert re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", rec["mac"]), f"{nid}: bad MAC {rec['mac']!r}"


def test_all_board_manifests_valid():
    """Every board's nodes.yaml loads + validates (c6 + s3-eth today)."""
    boards = [ROOT / "edge/esp32c6/nodes.yaml", ROOT / "edge/esp32s3-eth/nodes.yaml"]
    for mp in boards:
        nodes = EN.load(mp)
        assert nodes, f"{mp} is empty"
        for nid, rec in nodes.items():
            assert rec["sensor"] in EN.SENSORS, f"{mp}:{nid} bad sensor"


def test_known_nodes_and_their_sensors():
    nodes = EN.load()
    # The two live C6 gas nodes and their chips (SGP30 eCO2+TVOC = bedroom, SGP40 VOC = office).
    assert nodes["cbed_c6"]["sensor"] == "sgp30"
    assert nodes["coffice_c6"]["sensor"] == "sgp40"


def test_macs_are_unique():
    macs = [r["mac"] for r in EN.load().values()]
    assert len(macs) == len(set(macs)), "duplicate MAC in manifest — a flash gate would be ambiguous"


def test_sensor_define_mapping():
    assert "HA_GAS_SGP30" in EN.sensor_define("sgp30")
    assert EN.sensor_define("sgp40") is None          # SGP40 is the firmware default (no define)
    with raises(ValueError):
        EN.sensor_define("bme680")


def test_validate_rejects_missing_and_bad():
    with raises(ValueError):
        EN._validate("x", {"mac": "AA:BB", "target": "esp32c6"})       # missing fields
    with raises(ValueError):
        EN._validate("x", {**{k: "v" for k in EN.REQUIRED}, "sensor": "nope"})


def test_emit_bakes_sensor_and_identity():
    sgp30 = ENR.gen_secrets_h("cbed_c6", "s" * 64, "pw", "ssid", "psk",
                              "mqtt://192.168.0.210:1883", "pool.ntp.org",
                              sensor="sgp30", ota_host="192.168.0.210")
    assert '#define HA_NODE_ID      "cbed_c6"' in sgp30
    assert "#define HA_GAS_SGP30" in sgp30
    assert '#define HA_OTA_HOST     "192.168.0.210"' in sgp30

    sgp40 = ENR.gen_secrets_h("coffice_c6", "s" * 64, "pw", "ssid", "psk",
                              "mqtt://192.168.0.210:1883", "pool.ntp.org",
                              sensor="sgp40", ota_host="192.168.0.210")
    assert "HA_GAS_SGP30" not in sgp40, "SGP40 node must NOT define HA_GAS_SGP30"


def test_push_guard_reads_brand():
    p = _fake_app_bin("cbed_c6@v17")
    assert O.image_app_version(p) == "cbed_c6@v17"


def test_push_guard_matches_and_refuses():
    p = _fake_app_bin("cbed_c6@v17")
    O.assert_image_matches_node(p, "cbed_c6")             # right node -> no raise
    with raises(SystemExit):
        O.assert_image_matches_node(p, "coffice_c6")      # wrong node -> refuse
    O.assert_image_matches_node(p, "coffice_c6", force=True)  # --force overrides (no raise)


if __name__ == "__main__":
    raise SystemExit(run_module(globals()))
