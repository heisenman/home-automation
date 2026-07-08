#!/usr/bin/env python3
"""Per-node build/flash manifest for the ESP32-C6 edge fleet — the source of truth that binds a
`node_id` to its board wiring so a firmware image built for one node can never be flashed/OTA'd onto
another (the ADR-0020 cross-provisioning guard; see the 2026-07-05 cbed_c6<-coffice_c6 mixup).

Manifest: `edge/esp32c6/nodes.yaml`, `node_id -> {mac, target, sensor, area, broker, ota_host}`.
Consumed by:
  * tools/enroll_node.py  --from-manifest  (emit a complete, board-correct secrets.h — no hand-editing)
  * tools/node_bringup.py                  (drive build/flash/verify from the manifest)
  * the OTA/flash identity gate            (assert image node_id + chip MAC == the intended target)
"""
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO / "edge" / "esp32c6" / "nodes.yaml"

SENSORS = ("sgp30", "sgp40", "bme680")  # eCO2+TVOC / VOC-index / BME680 (T·RH·P·gas-Ω) — compile-time select
REQUIRED = ("mac", "target", "sensor", "area", "broker", "ota_host")


def sensor_define(sensor: str) -> str | None:
    """The secrets.h line that selects the gas driver. SGP40 is the firmware default (no define)."""
    if sensor == "sgp30":
        return "#define HA_GAS_SGP30            // Sensirion SGP30 (eCO2 + TVOC)"
    if sensor == "sgp40":
        return None                     # default path in ha_gas.c — no #define
    if sensor == "bme680":
        return "#define HA_GAS_BME680           // Bosch BME680 (T·RH·P + gas resistance, Ω)"
    raise ValueError(f"unknown sensor {sensor!r} (expected one of {SENSORS})")


def _validate(node_id: str, rec: dict) -> dict:
    missing = [k for k in REQUIRED if not rec.get(k)]
    if missing:
        raise ValueError(f"node {node_id!r}: missing manifest field(s): {', '.join(missing)}")
    if rec["sensor"] not in SENSORS:
        raise ValueError(f"node {node_id!r}: sensor {rec['sensor']!r} not in {SENSORS}")
    rec = dict(rec)
    rec["mac"] = str(rec["mac"]).upper()
    return rec


def load(path: Path = DEFAULT_MANIFEST) -> dict:
    """Return {node_id: record}, every record validated. Raises on malformed manifest."""
    raw = yaml.safe_load(Path(path).read_text()) or {}
    nodes = raw.get("nodes") or {}
    return {nid: _validate(nid, rec) for nid, rec in nodes.items()}


def get(node_id: str, path: Path = DEFAULT_MANIFEST) -> dict:
    """One node's validated record. Raises KeyError with a helpful message if absent."""
    nodes = load(path)
    if node_id not in nodes:
        raise KeyError(f"{node_id!r} not in manifest {path} (known: {', '.join(sorted(nodes)) or 'none'})")
    return nodes[node_id]
