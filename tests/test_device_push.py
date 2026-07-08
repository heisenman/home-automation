"""device_push: ESP32/edge 'node' class wiring — classify from edge/*/nodes.yaml + repoint via
tools/repoint_node.py (secrets via env, dry-run safe). Complements dev's repoint_node (@205fea6)."""
from server.maintenance import device_push as DP


def _edge(tmp_path):
    d = tmp_path / "esp32c6"; d.mkdir()
    (d / "nodes.yaml").write_text("nodes:\n  gas_test:\n    mac: aa:bb\n    area: office\n")
    return tmp_path


def test_classify_esp32_from_nodes_manifest(tmp_path):
    assert DP.classify("gas_test", edge_dir=_edge(tmp_path)) == "esp32"


def test_classify_unknown_when_not_a_node(tmp_path):
    assert DP.classify("ghost_node", edge_dir=_edge(tmp_path)) == "unknown"


def test_repoint_esp32_refuses_without_secret(monkeypatch):
    monkeypatch.delenv("HA_CMD_SECRET", raising=False)
    assert DP.repoint("gas_test", "esp32", dry=True) is False        # gated: no per-node secret


def test_repoint_esp32_refuses_without_psk(monkeypatch, tmp_path):
    monkeypatch.setenv("HA_CMD_SECRET", "s")
    monkeypatch.delenv("WIFI_PSK", raising=False)
    monkeypatch.setattr(DP, "REPO", tmp_path)                        # no airgap_router.env under tmp
    assert DP.repoint("gas_test", "esp32", dry=True) is False        # gated: no Wi-Fi PSK


def test_repoint_esp32_dry_run_builds_and_sends_nothing(monkeypatch):
    monkeypatch.setenv("HA_CMD_SECRET", "s"); monkeypatch.setenv("WIFI_PSK", "p")
    monkeypatch.setattr(DP, "resolve_node_id", lambda *a, **k: "gas_test")   # resolution has its own tests
    # dry-run must NOT spawn repoint_node; if it did, subprocess.run would be called — guard it
    monkeypatch.setattr(DP.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent!")))
    assert DP.repoint("gas_test", "esp32", dry=True) is True


def test_repoint_esp32_refuses_when_node_id_unresolvable(monkeypatch):
    monkeypatch.setenv("HA_CMD_SECRET", "s"); monkeypatch.setenv("WIFI_PSK", "p")
    monkeypatch.setattr(DP, "resolve_node_id", lambda *a, **k: None)         # device_id maps to no node
    assert DP.repoint("mystery_dev", "esp32", dry=True) is False


def test_resolve_node_id_gas_split(tmp_path):
    # the gas-node device_id<->node_id split: registry device_id 'gas_hbed' -> firmware node 'hbed_c6'
    # via a devices.yaml `node_id` field (NOT the reg key, which can be a legacy token).
    edge = tmp_path / "edge"; (edge / "esp32c6").mkdir(parents=True)
    (edge / "esp32c6" / "nodes.yaml").write_text("nodes:\n  hbed_c6:\n    mac: aa:bb\n")
    dev = tmp_path / "devices.yaml"
    dev.write_text('devices:\n  "hbed_c6-gas":\n    device_id: "gas_hbed"\n    node_id: "hbed_c6"\n')
    assert DP.resolve_node_id("gas_hbed", edge_dir=edge, devices_path=dev) == "hbed_c6"


def test_resolve_node_id_prefers_live_over_retired_dup(tmp_path):
    # two records share device_id gas_c_office (a retired node + its live replacement); resolve must pick
    # the node_id that is a CURRENT edge node in nodes.yaml, so a stale record can't win the repoint target.
    edge = tmp_path / "edge"; (edge / "esp32c6").mkdir(parents=True)
    (edge / "esp32c6" / "nodes.yaml").write_text("nodes:\n  coffice_c6:\n    mac: aa:bb\n")   # c6-bench retired: absent
    dev = tmp_path / "devices.yaml"
    dev.write_text('devices:\n'
                   '  "c6-bench-gas":\n    device_id: "gas_c_office"\n    node_id: "c6-bench"\n'
                   '  "coffice_c6-gas":\n    device_id: "gas_c_office"\n    node_id: "coffice_c6"\n')
    assert DP.resolve_node_id("gas_c_office", edge_dir=edge, devices_path=dev) == "coffice_c6"


def test_resolve_node_id_relay_style_device_is_own_node(tmp_path):
    # a plain relay node's device_id IS its node_id (no split)
    edge = tmp_path / "edge"; (edge / "esp32c6").mkdir(parents=True)
    (edge / "esp32c6" / "nodes.yaml").write_text("nodes:\n  cbed_c6:\n    mac: aa:bb\n")
    assert DP.resolve_node_id("cbed_c6", edge_dir=edge, devices_path=tmp_path / "none.yaml") == "cbed_c6"


def test_repoint_esp32_revert_not_driven_here(monkeypatch):
    monkeypatch.setenv("HA_CMD_SECRET", "s"); monkeypatch.setenv("WIFI_PSK", "p")
    assert DP.repoint("gas_test", "esp32", dry=True, revert=True) is False   # firmware boot-count reverts


def test_airgap_target_reads_psk_from_env(monkeypatch):
    monkeypatch.setenv("WIFI_PSK", "sekret")
    ssid, psk, broker, host = DP._airgap_target()
    assert ssid == "autohome_airgap" and psk == "sekret"
    assert broker == "mqtt://192.168.1.210:1883" and host == "192.168.1.210"
