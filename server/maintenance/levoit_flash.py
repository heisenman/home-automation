"""Server-side USB flashing of a Levoit Vital 200S (ESP32-C3-SOLO-1) with the GENERIC ESPHome image — the
"Levoit Vital 200S" kind of the PWA "Flash new hardware" panel. Design: docs/design/pwa-levoit-flashing.md.

Shares the transport with edge_flash (same .210 USB, same port lock, same admin job, same edge-manifest MAC
lookup) and nothing else: an edge node gets an IDF image + NVS identity blob + node-born secret, a Levoit gets
one ESPHome factory image and no secret at all (it is a server-driven actuator, adopted later via
server/esphome_intake.py). So this is its own module, importing edge_flash's pieces rather than growing it.

What this module is careful about:

* **The OEM backup gates the erase.** The full 4 MB is read and checked (size, not blank) and saved under
  instance/oem-backups/ BEFORE anything is erased. A unit whose OEM image was already backed up is not re-read —
  re-flashing a converted unit must never replace the real OEM backup with an ESPHome image.
* **One esptool session.** RTS auto-reset failed on 2 of 3 Levoits, so the operator puts the chip in download
  mode by hand (hold IO0, tap EN) and we connect with `no-reset` first. Backup → erase → write → verify run on
  that ONE connection: separate CLI calls would each try a reset, and a reset that works after IO0 was let go
  boots the OEM firmware in the middle of the sequence.
* **No compile here, ever.** The image is built ahead of time (tools/levoit_build_generic.sh) and carries no
  identity — each unit names itself levoit-<mac6> at boot (ESPHome name_add_mac_suffix).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from server.maintenance import edge_flash as EF
from server.maintenance.edge_flash import FlashError

REPO = EF.REPO
# ESPHome names the build dir after `esphome.name` (levoit), not after the yaml file.
BUILD_DIR = REPO / "provisioning" / "levoit" / ".esphome" / "build" / "levoit" / ".pioenvs" / "levoit"
IMAGE = BUILD_DIR / "firmware.factory.bin"
# Written by tools/levoit_build_generic.sh beside the image: {sha256, built, esphome}. The flasher refuses an
# image whose bytes no longer match it (a half-finished rebuild, or a per-unit build copied over it).
IMAGE_META = BUILD_DIR / "levoit-generic.build.json"
BACKUP_DIR = REPO / "instance" / "oem-backups"

CHIP = "ESP32-C3"
FLASH_BYTES = 0x400000                 # C3-SOLO-1: 4 MB embedded
FLASH_BAUD = 460800
NAME_PREFIX = "levoit"                 # levoit-generic.yaml `device_name`
# Byte markers seen in the backup, reported as a hint (not enforced: a converted unit has no VeSync bytes).
MARKERS = {"vesync": b"vesync", "levoit": b"levoit", "esphome": b"esphome"}


def _cmds():
    """esptool's v5 public API. A function so tests can substitute a fake."""
    import esptool.cmds
    return esptool.cmds


def mac6(mac: str) -> str:
    return mac.replace(":", "").lower()[-6:]


def predicted_name(mac: str) -> str:
    """The name the generic image gives itself: ESPHome appends the last 6 hex digits of the base MAC
    (App::pre_setup, name_add_mac_suffix). This is the name that shows up in Standby hardware."""
    return f"{NAME_PREFIX}-{mac6(mac)}"


# ── image ───────────────────────────────────────────────────────────────────────────────────────────

def image_status() -> dict:
    """Whether the generic image is flashable right now, and why not if it isn't. The survey shows this so the
    panel never offers a Flash button for an image that doesn't exist. Path + hash only, never the bytes (the
    image embeds Wi-Fi PSKs and the shared OTA key)."""
    row = {"kind": "levoit", "ready": False, "reason": None, "sha256": None, "built": None, "esphome": None,
           "image": str(IMAGE.relative_to(REPO)) if IMAGE.is_relative_to(REPO) else str(IMAGE)}
    if not IMAGE.exists():
        row["reason"] = "generic Levoit image not built — run tools/levoit_build_generic.sh on .210"
        return row
    if not IMAGE_META.exists():
        row["reason"] = ("image has no build record (levoit-generic.build.json) — rebuild with "
                         "tools/levoit_build_generic.sh so the flash is traceable to an exact artifact")
        return row
    meta = json.loads(IMAGE_META.read_text())
    sha = hashlib.sha256(IMAGE.read_bytes()).hexdigest()
    if sha != meta.get("sha256"):
        row["reason"] = ("image bytes don't match its build record — a rebuild is half-finished or the file was "
                         "replaced. Re-run tools/levoit_build_generic.sh")
        return row
    row.update(ready=True, sha256=sha[:16], built=meta.get("built"), esphome=meta.get("esphome"))
    return row


# ── OEM backup ──────────────────────────────────────────────────────────────────────────────────────

def existing_backup(mac: str) -> Path | None:
    """An OEM backup already on disk for this MAC whose bytes still match their sidecar, or None."""
    for p in sorted(BACKUP_DIR.glob(f"levoit-{mac6(mac)}-oem-*.bin")):
        side = p.with_suffix(".sha256")
        if side.exists() and side.read_text().split()[0] == hashlib.sha256(p.read_bytes()).hexdigest():
            return p
    return None


def markers(data: bytes) -> list[str]:
    low = data.lower()
    return sorted(k for k, m in MARKERS.items() if m in low)


def save_backup(data: bytes, mac: str) -> tuple[Path, list[str]]:
    """Check and store a full-flash read. Raises (so NOTHING gets erased) on a short or blank read.

    Named `-oem-` only if it actually looks like the VeSync firmware; anything else (e.g. a unit already on
    ESPHome with no earlier backup) is kept as `-preflash-`, so a later re-flash never mistakes it for the OEM
    image."""
    if len(data) != FLASH_BYTES:
        raise FlashError(f"backup read {len(data)} bytes, expected {FLASH_BYTES} — nothing erased")
    if data.count(0xFF) == len(data):
        raise FlashError("backup read is entirely blank (0xFF) — the read is not trustworthy, nothing erased")
    found = markers(data)
    label = "oem" if "vesync" in found else "preflash"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    out = BACKUP_DIR / f"levoit-{mac6(mac)}-{label}-{time.strftime('%Y-%m-%d')}.bin"
    n = 2
    while out.exists():
        out = out.with_name(f"levoit-{mac6(mac)}-{label}-{time.strftime('%Y-%m-%d')}-{n}.bin")
        n += 1
    tmp = out.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)   # may hold VeSync cloud creds
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    tmp.replace(out)
    out.with_suffix(".sha256").write_text(f"{hashlib.sha256(data).hexdigest()}  {out.name}\n")
    return out, found


# ── the flash ───────────────────────────────────────────────────────────────────────────────────────

def _connect(port: str, say):
    """Connect to a chip the operator has (probably) put in download mode by hand. `no-reset` first, because
    that is the case that works on every unit; `default-reset` (RTS) as a fallback for units where it works."""
    c = _cmds()
    errors = []
    for mode in ("no-reset", "default-reset"):
        try:
            esp = c.detect_chip(port, 115200, connect_mode=mode, connect_attempts=3)
            say(f"connected ({mode})")
            return esp
        except Exception as e:          # noqa: BLE001 — esptool raises FatalError/SerialException/OSError
            errors.append(f"{mode}: {str(e).strip().splitlines()[-1] if str(e).strip() else type(e).__name__}")
    raise FlashError("could not reach the chip's bootloader (" + "; ".join(errors) + "). Put it in download "
                     "mode: hold IO0→GND, tap EN→GND, release EN (IO0 can be released now), then Flash again.")


def flash_levoit(spec: dict, *, progress=None) -> dict:
    """Connect → identify → OEM backup (or reuse) → erase + write generic image → verify. ~2.5 min with a
    fresh backup, ~30 s when reusing one. `spec`: {port}."""
    say = progress or (lambda m: EF.log.info("%s", m))
    port = str(spec.get("port") or "")
    if not port.startswith("/dev/"):
        raise FlashError("port must be a /dev/tty* path")
    img = image_status()
    if not img["ready"]:
        raise FlashError(img["reason"])            # before touching the board
    c = _cmds()

    with EF._PortLock():
        say(f"connecting to {port}…")
        esp = _connect(port, say)
        try:
            if esp.CHIP_NAME != CHIP:
                raise FlashError(f"this is an {esp.CHIP_NAME}, not the {CHIP} a Vital 200S has — nothing written")
            mac = ":".join(f"{b:02X}" for b in esp.read_mac())
            known = EF.known_node_for_mac(mac)
            if known:
                raise FlashError(f"{mac} is our own edge node '{known['node_id']}' ({known['manifest']}), not a "
                                 f"Levoit — use the Edge node kind. Nothing written.")
            esp = c.run_stub(esp)
            esp.change_baud(FLASH_BAUD)
            c.attach_flash(esp)
            size = c.detect_flash_size(esp)
            if size != "4MB":
                raise FlashError(f"flash is {size}, expected 4MB (C3-SOLO-1) — not a Vital 200S? Nothing written.")
            say(f"found {CHIP} mac={mac} flash={size} → will appear as {predicted_name(mac)}")

            backup = existing_backup(mac)
            if backup:
                say(f"OEM backup already on file ({backup.name}) — not re-reading")
                found = None
            else:
                say("backing up the existing firmware (4 MB, ~2 min) — do not unplug…")
                data = c.read_flash(esp, 0, FLASH_BYTES, None, no_progress=True)
                backup, found = save_backup(bytes(data or b""), mac)
                say(f"backup saved: {backup.name} (markers: {', '.join(found) or 'none'})")

            say(f"erasing + writing generic image (sha {img['sha256']})…")
            c.write_flash(esp, [(0, str(IMAGE))], erase_all=True, no_progress=True)
            c.verify_flash(esp, [(0, str(IMAGE))])
            say("written and hash-verified")
            try:
                c.reset_chip(esp, "hard-reset")
            except Exception:                       # noqa: BLE001 — RTS may not be wired; power cycle covers it
                pass
        finally:
            try:
                esp._port.close()
            except Exception:                       # noqa: BLE001
                pass

    name = predicted_name(mac)
    return {"status": "flashed", "kind": "levoit", "mac": mac, "name": name,
            "image_sha256": img["sha256"], "backup": backup.name, "backup_markers": found,
            "next": (f"release IO0, unplug the programmer, reassemble and plug into mains. '{name}' appears in "
                     f"Standby hardware once the purifier is on mains (no PM2.5 on programmer power) — "
                     f"then Adopt it into a room.")}
