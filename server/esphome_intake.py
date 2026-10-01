"""Adopt an unregistered ESPHome air purifier (a reflashed Levoit) into a room — the server-side half of the
PWA "Standby hardware → Adopt" flow for ESPHome appliances (discovery: server/ingest/esphome_discovery.py).

A purifier is a SERVER-DRIVEN actuator (control.yaml `node: server`, routed by device_id to
LevoitMqttTransport), so adopting one is three registry entries, written in this order:

  1. control_secrets.yaml  — a fresh per-device command secret (the issuer signs every command, even for
                             local-driver devices whose transport ignores the signature). Never returned.
  2. control.yaml          — device_id, node: server, area, device_type: air_purifier, the purifier traits.
  3. levoit-devices.yaml   — ESPHome name -> device_id. LAST, because the Levoit bridge hot-reloads this file
                             and starts publishing the moment it lands; by then 1+2 already exist.

Every write is an APPEND of text, then a re-parse that must equal (old content + exactly the new entry), else
the original bytes are restored. Appending — not safe_dump-rewriting — keeps control.yaml's inline comments
(the design notes live there), and it means the writer never has to assume the file's layout matches this
box's: ha-2's copy is checked by the same parse, not by a template. A failure at step N restores steps <N.

Deliberately NOT done (docs/CONFORMANCE.md §B, ADR-0014 R2/R4): no automation policy is seeded. A purifier's
own PM2.5 sensor is never an automatic control source — the operator picks the source in the automation
editor. Manual control works immediately after the restart.

Taking effect needs ha-controller + ha-api + ha-api-tls restarted (the command plane and the Levoit
transport are built at boot); the caller does that out-of-process via admin_job op 'restart_control'.
"""
from __future__ import annotations

import os
import secrets as pysecrets
from pathlib import Path
from typing import Any

import yaml

from server.device_registry import SLUG_RE
from server.ingest.esphome_discovery import NAME_RE

# Verified live on levoit-office 2026-06-27 (fan 1-4) and levoit-c-office 2026-10-01 (on/off, CADR 0->103).
# Same Vital 200S firmware -> same trait set; CONFORMANCE §B R3 range verification is per unit at intake.
PURIFIER_TRAITS: dict[str, dict] = {
    "switchable": {"safe_on": False},          # fan power; fail-safe OFF
    "ranged": {"min": 1, "max": 4, "step": 1},  # fan speed 1-4
    "indicator": {"safe_on": True},             # panel LED (ESPHome display switch); night mode turns it off
}
RESTART_SERVICES = ["ha-controller", "ha-api", "ha-api-tls"]


class IntakeError(Exception):
    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def purifier_device_id(taken: set[str], area: str) -> str:
    """purifier_<area>; a second purifier in the same room gets _2, _3… (deterministic, always terminates)."""
    base = f"purifier_{area}"
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def _indent(text: str, n: int) -> str:
    pad = " " * n
    return "".join(pad + ln if ln.strip() else ln for ln in text.splitlines(keepends=True))


def _append_verified(path: Path, text: str, add: dict, *, under: str | None = None,
                     mode: int | None = None) -> bytes | None:
    """Append `text` to `path`, then require parse(after) == parse(before) with `add` merged at the top level
    (or under key `under`). Restores the original on mismatch. Returns the original bytes (None if the file
    did not exist) so a LATER failing step can roll this one back. Atomic via tmp+rename; keeps the file's
    mode (or uses `mode` for a new file)."""
    orig = path.read_bytes() if path.exists() else None
    before = _load(path)
    want = dict(before)
    if under:
        want[under] = {**(before.get(under) or {}), **add}
    else:
        want.update(add)
    body = (orig or b"").decode()
    if body and not body.endswith("\n"):
        body += "\n"
    new_mode = (os.stat(path).st_mode & 0o777) if orig is not None else (mode if mode is not None else 0o664)
    tmp = path.with_suffix(path.suffix + ".intake-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, new_mode)
    with os.fdopen(fd, "w") as f:
        f.write(body + text)
    os.chmod(tmp, new_mode)                    # O_CREAT mode is masked by umask; force it
    tmp.replace(path)
    try:
        ok = _load(path) == want
    except yaml.YAMLError:                     # the append broke the file outright — same remedy
        ok = False
    if not ok:
        _restore(path, orig)
        raise IntakeError(500, f"{path.name}: appended entry did not parse back as expected — restored; "
                               "the file's layout needs a hand-edit (is the mapping the LAST top-level key?)")
    return orig


def _restore(path: Path, orig: bytes | None) -> None:
    if orig is None:
        path.unlink(missing_ok=True)
    else:
        mode = os.stat(path).st_mode & 0o777 if path.exists() else 0o600
        tmp = path.with_suffix(path.suffix + ".intake-tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(orig)
        os.chmod(tmp, mode)
        tmp.replace(path)


def handle_adopt_purifier(name: str, body: dict[str, Any], *, control_path: Path, secrets_path: Path,
                          levoit_path: Path, areas_path: Path | None = None) -> tuple[int, dict]:
    """Validate, then write the three registry entries (or preview with dry_run). (code, payload)."""
    b = body or {}
    area = str(b.get("area", "")).strip()
    dry_run = bool(b.get("dry_run", False))
    try:
        if not NAME_RE.match(name or ""):
            raise IntakeError(400, "not an ESPHome node name")
        if not SLUG_RE.match(area):
            raise IntakeError(400, "area must be a slug [a-z0-9_]")
        if areas_path is not None and areas_path.exists():
            canon = set((_load(areas_path).get("areas") or {}))
            if canon and area not in canon:
                raise IntakeError(400, f"area '{area}' is not a canonical areas.yaml room")
        levoit = _load(levoit_path)
        if name in levoit:
            raise IntakeError(409, f"'{name}' is already registered as {levoit[name].get('device_id')}")
        control = (_load(control_path).get("devices") or {})
        secret_ids = set(_load(secrets_path))
        taken = set(control) | secret_ids | {str((v or {}).get("device_id")) for v in levoit.values()}
        device_id = purifier_device_id(taken, area)
    except IntakeError as e:
        return e.code, {"status": "bad-request" if e.code == 400 else "conflict", "node": name,
                        "reason": str(e)}

    ctl_entry = {"node": "server", "area": area, "device_type": "air_purifier",
                 "traits": {k: dict(v) for k, v in PURIFIER_TRAITS.items()}}
    lev_entry = {"device_id": device_id, "device_type": "air_purifier"}
    plan = {"node": name, "device_id": device_id, "area": area,
            "writes": [secrets_path.name, control_path.name, levoit_path.name],
            "restart": RESTART_SERVICES,
            "automation": "none seeded — pick a source sensor in the automation editor (CONFORMANCE §B R2/R4)"}
    if dry_run:
        return 200, {"status": "preview", "dry_run": True, **plan}

    done: list[tuple[Path, bytes | None]] = []
    try:
        secret = pysecrets.token_hex(32)
        done.append((secrets_path, _append_verified(
            secrets_path, f"{device_id}: {secret}   # ESPHome intake ({name})\n", {device_id: secret},
            mode=0o600)))
        ctl_text = (f"  # Air purifier — ESPHome '{name}', adopted via PWA Standby-hardware intake.\n"
                    + _indent(yaml.safe_dump({device_id: ctl_entry}, sort_keys=False), 2))
        done.append((control_path, _append_verified(control_path, ctl_text, {device_id: ctl_entry},
                                                    under="devices")))
        done.append((levoit_path, _append_verified(
            levoit_path, yaml.safe_dump({name: lev_entry}, sort_keys=False), {name: lev_entry})))
    except Exception as e:                           # roll back whatever already landed, newest first
        for p, orig in reversed(done):
            try:
                _restore(p, orig)
            except Exception:
                pass
        code = e.code if isinstance(e, IntakeError) else 500
        return code, {"status": "error", "node": name, "reason": str(e), "rolled_back": [p.name for p, _ in done]}
    return 201, {"status": "registered", **plan}
