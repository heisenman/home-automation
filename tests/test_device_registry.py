"""Tests for the sensor add-device backend (server/device_registry.py + the POST /api/v1/devices route)."""
import textwrap

from pathlib import Path

from server.device_registry import (handle_add_actuator, handle_add_device, load_control_devices,
                                     load_devices, validate_new_actuator, validate_new_device)


def _seed_control(tmp_path: Path) -> Path:
    f = tmp_path / "control.yaml"
    f.write_text(textwrap.dedent('''\
        # Actuator control registry — keep header
        version: 1
        devices:
          lamp_office:
            node: c6-bench
            area: c_office
            traits:
              switchable: {}
    '''))
    return f


def test_actuator_append_preserves_version_and_header(tmp_path):
    f = _seed_control(tmp_path)
    code, p = handle_add_actuator(f, {"device_id": "lock_back", "node": "c6-bench", "area": "entry",
                                      "traits": {"lockable": {}}})
    assert code == 201 and p["reload_required"]
    txt = f.read_text()
    assert txt.startswith("# Actuator control registry") and "version: 1" in txt and "lamp_office" in txt
    assert load_control_devices(f)["lock_back"]["node"] == "c6-bench"
    assert (tmp_path / "control.yaml.bak").exists()


def test_enroll_node_atomic_and_non_destructive(tmp_path):
    from server.control import secret_store as ss
    from server.device_registry import handle_enroll_node
    enc = tmp_path / "node_secrets.enc"
    master = "test-master"
    ss.save_lut(enc, master, {"c6-bench": {"mac": "AA:BB:CC:DD:EE:01", "cmd_secret": "existing", "created": 1}})
    code, p = handle_enroll_node(enc, master, {"node_id": "s3-attic", "mac": "aa:bb:cc:dd:ee:99"})
    assert code == 201 and len(p["cmd_secret"]) == 64 and p["secrets_h"].startswith("#define HA_CMD_SECRET")
    lut = ss.load_lut(enc, master)
    assert set(lut) == {"c6-bench", "s3-attic"}
    assert lut["c6-bench"]["cmd_secret"] == "existing"          # existing node untouched
    assert lut["s3-attic"]["mac"] == "AA:BB:CC:DD:EE:99"        # normalised
    assert (tmp_path / "node_secrets.enc.bak").exists()
    assert handle_enroll_node(enc, master, {"node_id": "c6-bench"})[0] == 400          # dup
    assert handle_enroll_node(enc, master, {"node_id": "Bad Node"})[0] == 400          # bad slug
    assert handle_enroll_node(enc, master, {"node_id": "ok", "mac": "nope"})[0] == 400  # bad mac
    assert handle_enroll_node(enc, "WRONG", {"node_id": "x"})[0] == 500                 # wrong master -> no overwrite


def test_slug_allows_hyphen(tmp_path):
    from server.device_registry import validate_new_device
    entry, err = validate_new_device({"mac": "AA:BB:CC:DD:EE:05", "device_id": "meter-den",
                                      "device_type": "t", "area": "den"}, {})
    assert err is None and entry["device_id"] == "meter-den"


def test_actuator_rejects_bad_trait_dup_and_missing(tmp_path):
    f = _seed_control(tmp_path)
    assert handle_add_actuator(f, {"device_id": "x", "node": "n", "area": "a", "traits": {"frobnicate": {}}})[0] == 400
    assert handle_add_actuator(f, {"device_id": "lamp_office", "node": "n", "area": "a", "traits": {"switchable": {}}})[0] == 400
    assert handle_add_actuator(f, {"device_id": "y", "node": "n", "area": "a", "traits": {}})[0] == 400
    assert handle_add_actuator(f, {"device_id": "z", "node": "", "area": "a", "traits": {"switchable": {}}})[0] == 400
    code, p = handle_add_actuator(f, {"device_id": "good_lamp", "node": "c6-bench", "area": "den", "traits": {"switchable": {}, "ranged": {"min": 0, "max": 100}}})
    assert code == 201


def _seed(tmp_path: Path) -> Path:
    f = tmp_path / "devices.yaml"
    f.write_text(textwrap.dedent('''\
        # Device registry — keep this header
        # restart ha-scanner after edits
        devices:
          "AA:BB:CC:DD:EE:01":
            device_id: meter_existing
            device_type: switchbot_meter
            area: kitchen
            notes: "keep me"
    '''))
    return f


def test_load_devices_uppercases_macs(tmp_path):
    f = _seed(tmp_path)
    devs = load_devices(f)
    assert "AA:BB:CC:DD:EE:01" in devs
    assert devs["AA:BB:CC:DD:EE:01"]["device_id"] == "meter_existing"


def test_validate_rejects_bad_input():
    existing = {}
    assert validate_new_device({"mac": "nope", "device_id": "x", "device_type": "t", "area": "a"}, existing)[1]
    assert validate_new_device({"mac": "AA:BB:CC:DD:EE:02", "device_id": "Bad ID", "device_type": "t", "area": "a"}, existing)[1]
    assert validate_new_device({"mac": "AA:BB:CC:DD:EE:02", "device_id": "ok", "device_type": "", "area": "a"}, existing)[1]
    assert validate_new_device({"mac": "AA:BB:CC:DD:EE:02", "device_id": "ok", "device_type": "t", "area": "Bad Area"}, existing)[1]
    entry, err = validate_new_device({"mac": "aa:bb:cc:dd:ee:02", "device_id": "ok", "device_type": "t", "area": "den"}, existing)
    assert err is None and entry["mac"] == "AA:BB:CC:DD:EE:02"   # normalised upper


def test_add_appends_preserves_header_and_backs_up(tmp_path):
    f = _seed(tmp_path)
    code, payload = handle_add_device(f, {"mac": "aa:bb:cc:dd:ee:02", "device_id": "meter_den",
                                          "device_type": "switchbot_meter_pro", "area": "den",
                                          "capabilities": ["temperature", "humidity"]})
    assert code == 201 and payload["status"] == "registered" and payload["reload_required"]
    txt = f.read_text()
    assert txt.startswith("# Device registry")          # header preserved
    assert "keep me" in txt                              # existing notes preserved
    devs = load_devices(f)
    assert devs["AA:BB:CC:DD:EE:02"]["capabilities"] == ["temperature", "humidity"]
    assert (tmp_path / "devices.yaml.bak").exists()      # atomic write kept a backup


def test_add_rejects_duplicate_mac_and_id(tmp_path):
    f = _seed(tmp_path)
    code, p = handle_add_device(f, {"mac": "AA:BB:CC:DD:EE:01", "device_id": "x", "device_type": "t", "area": "a"})
    assert code == 400 and "already registered" in p["reason"]
    code, p = handle_add_device(f, {"mac": "AA:BB:CC:DD:EE:09", "device_id": "meter_existing", "device_type": "t", "area": "a"})
    assert code == 400 and "already in use" in p["reason"]
