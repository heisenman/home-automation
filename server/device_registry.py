"""Read + append the SENSOR device registry (`instance/devices.yaml`) — the source of truth keyed by
uppercase MAC, consumed by ha-scanner / ha-edge-mapper to map a heard advert to a canonical
`home/<area>/<device_id>/state` topic.

Kept separate from the ingest loaders (which only READ it at startup) so the API can append without
importing ingest. NOTE: scanner/mapper load this ONCE at boot — there is no hot-reload, so an append must
be followed by `systemctl restart ha-scanner ha-edge-mapper` to take effect (the handler says so).

Append is atomic (tmp+rename), keeps a `.bak`, and preserves the file's leading comment block (Hugh's
hand-maintained header) since PyYAML's safe_dump would otherwise drop comments.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
SLUG_RE = re.compile(r"^[a-z0-9_-]+$")   # allow hyphens — real node ids use them (c6-bench, s3-crawlspace)


def load_devices(path: Path) -> dict[str, dict]:
    """{MAC(upper): info}. Missing/empty file -> {}."""
    if not path.exists():
        return {}
    with path.open() as f:
        raw = yaml.safe_load(f) or {}
    return {str(m).upper(): info for m, info in (raw.get("devices") or {}).items()}


def validate_new_device(body: dict[str, Any], existing: dict[str, dict]) -> tuple[dict | None, str | None]:
    """Validate a new SENSOR registration. Returns (entry, None) or (None, error). `entry` carries `mac`
    separately from the stored fields (the caller keys the YAML map by mac)."""
    b = body or {}
    mac = str(b.get("mac", "")).strip().upper()
    if not MAC_RE.match(mac):
        return None, "mac must be a colon-separated MAC, e.g. AA:BB:CC:DD:EE:FF"
    device_id = str(b.get("device_id", "")).strip()
    if not SLUG_RE.match(device_id):
        return None, "device_id must be a slug [a-z0-9_]"
    device_type = str(b.get("device_type", "")).strip()
    if not device_type:
        return None, "device_type required (e.g. switchbot_meter_pro, aranet_radon_plus)"
    area = str(b.get("area", "")).strip()
    if not SLUG_RE.match(area):
        return None, "area must be a slug [a-z0-9_] (e.g. living_room)"
    if mac in existing:
        return None, f"MAC {mac} already registered as '{existing[mac].get('device_id')}'"
    if any((i or {}).get("device_id") == device_id for i in existing.values()):
        return None, f"device_id '{device_id}' already in use"
    entry: dict[str, Any] = {"mac": mac, "device_id": device_id, "device_type": device_type, "area": area}
    caps = b.get("capabilities")
    if caps is not None:
        if not isinstance(caps, list) or not all(isinstance(c, str) for c in caps):
            return None, "capabilities must be a list of strings"
        entry["capabilities"] = caps
    notes = b.get("notes")
    if notes is not None:
        if not isinstance(notes, str):
            return None, "notes must be a string"
        entry["notes"] = notes
    return entry, None


def _leading_comments(path: Path) -> str:
    """The contiguous leading comment/blank block at the top of the file (preserved across rewrite)."""
    if not path.exists():
        return ""
    out: list[str] = []
    with path.open() as f:
        for line in f:
            if line.lstrip().startswith("#") or line.strip() == "":
                out.append(line)
            else:
                break
    return "".join(out)


def _write_yaml_preserving(path: Path, raw: dict[str, Any]) -> None:
    """Atomic write (tmp+rename), keep <file>.bak, preserve the file's leading comment header (safe_dump
    would otherwise drop comments)."""
    header = _leading_comments(path)
    if path.exists():
        path.with_suffix(path.suffix + ".bak").write_bytes(path.read_bytes())
    body = yaml.safe_dump(raw, sort_keys=False, default_flow_style=False, allow_unicode=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text((header + body) if header else body)
    tmp.replace(path)


def append_device(path: Path, entry: dict[str, Any]) -> None:
    """Append one validated SENSOR device (keyed by MAC). Atomic + .bak + header-preserving."""
    raw: dict[str, Any] = {}
    if path.exists():
        with path.open() as f:
            raw = yaml.safe_load(f) or {}
    e = dict(entry)
    mac = e.pop("mac")
    raw.setdefault("devices", {})[mac] = e
    _write_yaml_preserving(path, raw)


# ── actuator (control.yaml) registration ────────────────────────────────────────────────────────────
def load_control_devices(path: Path) -> dict[str, dict]:
    """{device_id: {node, area, traits}} from control.yaml. Missing -> {}."""
    if not path.exists():
        return {}
    with path.open() as f:
        return (yaml.safe_load(f) or {}).get("devices") or {}


def validate_new_actuator(body: dict[str, Any], existing: dict[str, dict]) -> tuple[dict | None, str | None]:
    """Validate a new ACTUATOR for control.yaml. Trait names are checked against the ADR-0002 trait set.
    Returns (entry, None) or (None, error)."""
    from server.control import traits as traits_mod
    b = body or {}
    device_id = str(b.get("device_id", "")).strip()
    if not SLUG_RE.match(device_id):
        return None, "device_id must be a slug [a-z0-9_]"
    if device_id in existing:
        return None, f"device_id '{device_id}' already in control.yaml"
    node = str(b.get("node", "")).strip()
    if not node:
        return None, "node required (the enrolled node that signs for this device)"
    area = str(b.get("area", "")).strip()
    if not SLUG_RE.match(area):
        return None, "area must be a slug [a-z0-9_]"
    traits = b.get("traits")
    if not isinstance(traits, dict) or not traits:
        return None, "traits must be a non-empty object, e.g. {\"onoff\": {}}"
    try:
        traits_mod.validate_device_traits(list(traits))
    except Exception as e:  # validate_device_traits raises on unknown trait
        return None, f"invalid traits: {e}"
    if not all(isinstance(c, dict) or c is None for c in traits.values()):
        return None, "each trait's config must be an object or null"
    return {"device_id": device_id, "node": node, "area": area,
            "traits": {t: (c or {}) for t, c in traits.items()}}, None


def append_control_device(path: Path, entry: dict[str, Any]) -> None:
    """Append one validated actuator to control.yaml (keyed by device_id). Atomic + .bak + header-preserving."""
    raw: dict[str, Any] = {}
    if path.exists():
        with path.open() as f:
            raw = yaml.safe_load(f) or {}
    e = dict(entry)
    device_id = e.pop("device_id")
    raw.setdefault("devices", {})[device_id] = e
    _write_yaml_preserving(path, raw)


def handle_enroll_node(node_secrets_path: Path, master: str, body: dict[str, Any]) -> tuple[int, dict]:
    """Enroll a NEW node: mint its cmd_secret + mqtt creds, atomically re-encrypt node_secrets.enc (keep a
    .bak), and VERIFY the round-trip decrypt — rolling back on any failure so a corrupt write can never
    report success (this LUT gates ALL node command auth). Returns the secret + a secrets.h snippet for the
    operator to flash. (Hugh authorised API enrolment 2026-06-25; was console-only before.)"""
    import secrets as pysecrets
    import time as _time

    from server.control import secret_store as ss
    b = body or {}
    node_id = str(b.get("node_id", "")).strip()
    if not SLUG_RE.match(node_id):
        return 400, {"status": "bad-request", "reason": "node_id must be a slug [a-z0-9_]"}
    mac = str(b.get("mac", "")).strip().upper()
    if mac and not MAC_RE.match(mac):
        return 400, {"status": "bad-request", "reason": "mac (optional) must be AA:BB:CC:DD:EE:FF"}
    try:
        lut = ss.load_lut(node_secrets_path, master)
    except ValueError as e:
        return 500, {"status": "error", "reason": str(e)}
    if node_id in lut:
        return 400, {"status": "bad-request", "reason": f"node '{node_id}' already enrolled"}
    cmd_secret = pysecrets.token_hex(32)
    lut[node_id] = {"mac": mac, "cmd_secret": cmd_secret, "mqtt_user": node_id,
                    "mqtt_pass": pysecrets.token_hex(16), "created": int(_time.time())}
    p = Path(node_secrets_path)
    bak = p.with_suffix(p.suffix + ".bak")
    if p.exists():
        bak.write_bytes(p.read_bytes())
    ss.save_lut(p, master, lut)                       # atomic (tmp+rename)
    try:                                               # never report success on a corrupt write
        if ss.load_lut(p, master).get(node_id, {}).get("cmd_secret") != cmd_secret:
            raise ValueError("verify mismatch")
    except Exception:
        if bak.exists():
            p.write_bytes(bak.read_bytes())            # roll back to the pre-write LUT
        return 500, {"status": "error", "reason": "enrolment failed round-trip verification; rolled back"}
    return 201, {
        "status": "enrolled",
        "node_id": node_id,
        "cmd_secret": cmd_secret,
        "secrets_h": f'#define HA_CMD_SECRET "{cmd_secret}"',
        "note": ("Node enrolled in node_secrets.enc. Bake the secrets_h line into the node's secrets.h, "
                 "build + flash, then add its device(s) via POST /api/v1/control-devices with node='" +
                 node_id + "'. mqtt creds are stored for the air-gap broker-auth cutover (broker is anon "
                 "until then). Keep this secret out of git."),
    }


def handle_add_actuator(path: Path, body: dict[str, Any]) -> tuple[int, dict]:
    """Pure: validate + append an actuator to control.yaml. The per-device command secret is derived from
    the node's existing cmd_secret (node_secrets.enc) — so the node must already be enrolled, or the device
    will be declared-but-uncommandable (the controller logs that loudly at boot)."""
    existing = load_control_devices(path)
    entry, err = validate_new_actuator(body, existing)
    if err:
        return 400, {"status": "bad-request", "reason": err}
    append_control_device(path, entry)
    return 201, {
        "status": "registered",
        "device_id": entry["device_id"],
        "node": entry["node"],
        "reload_required": True,
        "reload_cmd": "sudo systemctl restart ha-controller ha-api",
        "note": ("Declared in control.yaml. Its command secret is derived from node '" + entry["node"] +
                 "'s cmd_secret — that node must already be enrolled (node_secrets.enc) or the device is "
                 "uncommandable (the controller warns at boot). Restart ha-controller + ha-api to load it, "
                 "then verify: python3 tools/device_smoke_test.py " + entry["device_id"] +
                 " --command '<trait>:set:...'. Enrolling a NEW node is a separate step."),
    }


def handle_add_device(path: Path, body: dict[str, Any]) -> tuple[int, dict]:
    """Pure: validate + append a sensor device. Returns (status_code, payload)."""
    existing = load_devices(path)
    entry, err = validate_new_device(body, existing)
    if err:
        return 400, {"status": "bad-request", "reason": err}
    append_device(path, entry)
    return 201, {
        "status": "registered",
        "device_id": entry["device_id"],
        "mac": entry["mac"],
        "reload_required": True,
        "reload_cmd": "sudo systemctl restart ha-scanner ha-edge-mapper",
        "note": ("Registered in devices.yaml. ha-scanner/ha-edge-mapper load the registry only at "
                 "startup, so restart them to begin decoding this device to home/<area>/<device_id>/state, "
                 "then verify with: python3 tools/device_smoke_test.py " + entry["device_id"]),
    }
