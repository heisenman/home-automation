"""Server-side USB flashing of edge MCUs (Phase 1 of docs/design/pwa-firmware-loading.md).

Hugh's decision 2026-08-01: flashing happens **server-side on `.210` only** — the one box with reliable
USB port access — not over WebSerial. That also gives the best security posture of the three transports
we costed: nothing sensitive crosses a network, and the command secret is never handled here at all
(it is node-born, ADR-0036 Layer 0).

The model this depends on (Phase 0, `412da0a`): **one generic image per target, identity in NVS.**

    generic image (app_desc "generic@<target>")  +  16 KB NVS blob @ 0x9000  =  an identified node

So flashing is: work out what is plugged in → mint a blob for THAT chip → write both. No rebuild, ever.

Three things this module is careful about:

* **Identity is bound to silicon.** The blob carries `bind_mac`, read from the chip on the cable at flash
  time, and the firmware refuses to boot if it does not match (`ha_config_identity_ok`). That is the
  ADR-0020 anti-cross-provisioning gate moved to where it now belongs — stronger than the old one, which
  trusted a manifest line a human typed.
* **One flash at a time.** A serial port is not shareable and a half-written flash bricks a board until
  someone re-cables it, so every operation takes an exclusive lock.
* **Re-flashing an enrolled node orphans it.** Wiping NVS mints a new node-born secret while the LUT still
  holds the old one, and the ADR-0036 TOFU-lock (correctly) refuses to overwrite — leaving a node nobody
  can command. Policy (Hugh, 2026-08-01): **auto-rotate, but only on explicit confirmation.** Holding the
  cable is the physical-presence trust root (ADR-0010/0011), so it is sufficient authority; it is not
  something to do silently, because it means a cable alone can take over an existing identity.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

log = logging.getLogger("ha.edge_flash")

REPO = Path(__file__).resolve().parents[2]
LOCK_PATH = REPO / "instance" / ".edge-flash.lock"

# Toolchain that lives outside the repo venv. nvs_partition_gen needs the IDF's own python env (its
# esp_idf_nvs_partition_gen module is not in our venv, and the venv is shared — adding packages there
# has bitten us before, see the esphome/paho pin), so we invoke it by absolute path rather than importing.
def _real_home() -> Path:
    """The account's home from /etc/passwd — NOT $HOME.

    Both ha-api and ha-admin-job set `Environment=HOME=<repo>/instance` (so stray tool state lands in the
    instance dir rather than the real home), which makes Path.home() resolve to `…/instance` and every
    toolchain path built from it wrong. Found the hard way: the first API-driven flash died with
    "nvs_partition_gen.py not found at …/instance/esp/esp-idf/…". getpwuid ignores the env override.
    """
    import pwd
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:                      # no passwd entry (container-ish) — fall back to $HOME
        return Path(os.path.expanduser("~"))


IDF_PATH = Path(os.environ.get("IDF_PATH") or (_real_home() / "esp" / "esp-idf"))
IDF_PYTHON = Path(os.environ.get("HA_IDF_PYTHON")
                  or (_real_home() / ".espressif" / "python_env" / "idf5.4_py3.13_env" / "bin" / "python"))
NVS_GEN = IDF_PATH / "components" / "nvs_flash" / "nvs_partition_generator" / "nvs_partition_gen.py"

NVS_OFFSET = "0x9000"
NVS_SIZE = 0x4000                     # must equal the `nvs` row in the target's partitions.csv

# Where each target's generic image lives, and the flash layout. Phase 3 replaces this with a built
# manifest; until then we read the build tree directly and verify the artifact before writing it.
TARGETS = {
    "esp32c6":     {"dir": "edge/esp32c6",     "app": "ha-edge-c6.bin"},
    "esp32s3":     {"dir": "edge/esp32s3-eth", "app": "ha-edge-s3-eth.bin"},
    "esp32c3":     {"dir": "edge/esp32c3",     "app": "ha-edge-c3.bin"},
}

NODE_RE = re.compile(r"^[a-z0-9_]+$")
MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
GAS_CHOICES = ("auto", "sgp40", "sgp30", "bme680", "none")


class FlashError(Exception):
    """Operator-facing failure. The message is shown in the PWA verbatim."""


class _PortLock:
    """Exclusive, non-blocking lock around one flash operation.

    A serial port cannot be shared, and two concurrent writes leave a board bricked until someone
    re-cables it — which on a deployed node means a trip. Fail fast and tell the caller who holds it
    rather than queueing, because the holder is a human at a bench, not a job that will drain.
    """

    def __init__(self, path: Path = LOCK_PATH):
        self.path = path
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._fh.seek(0)
            holder = self._fh.read().strip() or "another operation"
            self._fh.close()
            self._fh = None
            raise FlashError(f"a flash is already in progress ({holder}) — one board at a time")
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(f"pid={os.getpid()} since={time.strftime('%H:%M:%SZ', time.gmtime())}")
        self._fh.flush()
        return self

    def __exit__(self, *exc):
        if self._fh:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
        return False


def _run(cmd: list[str], timeout: float = 300.0) -> str:
    """Run a toolchain command, returning stdout. Raises FlashError with the tail of the output — the
    esptool failure that matters (wrong port, board not in bootloader, cable) is always in the last lines."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(REPO))
    except subprocess.TimeoutExpired:
        raise FlashError(f"timed out after {timeout:.0f}s: {' '.join(cmd[:3])}…")
    except FileNotFoundError:
        raise FlashError(f"tool not found: {cmd[0]}")
    if p.returncode != 0:
        tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-6:])
        raise FlashError(f"{Path(cmd[0]).name} failed (rc={p.returncode}):\n{tail}")
    return p.stdout


def _esptool(*args: str, port: str | None = None, timeout: float = 300.0) -> str:
    import sys
    cmd = [sys.executable, "-m", "esptool"]
    if port:
        cmd += ["--port", port]
    return _run(cmd + list(args), timeout=timeout)


# ── discovery ────────────────────────────────────────────────────────────────────────────────────────

def list_ports() -> list[dict]:
    """Serial ports that look like an ESP dev board. The by-id symlink carries the chip's MAC for
    Espressif USB-JTAG devices, so we can often identify a board without touching it."""
    out = []
    by_id = Path("/dev/serial/by-id")
    if by_id.is_dir():
        for link in sorted(by_id.iterdir()):
            try:
                dev = str((by_id / os.readlink(link)).resolve())
            except OSError:
                continue
            m = re.search(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})", link.name)
            out.append({"port": dev, "id": link.name,
                        "mac_hint": m.group(1).upper() if m else None})
    if not out:                                   # boards behind a plain USB-UART have no by-id MAC
        for pat in ("/dev/ttyACM*", "/dev/ttyUSB*"):
            import glob
            for dev in sorted(glob.glob(pat)):
                out.append({"port": dev, "id": Path(dev).name, "mac_hint": None})
    return out


def detect(port: str) -> dict:
    """Identify the attached chip: family, revision, and base MAC — read from the silicon, not guessed.

    This is what makes the PWA a confirmation surface rather than a data-entry form: the operator does not
    have to know which board they plugged in. Read-only; leaves the chip running."""
    txt = _esptool("read_mac", port=port, timeout=60)
    txt = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", txt)      # esptool 5.x colourises and redraws lines
    # esptool 4.x says "Chip is ESP32-C6FH4 (QFN32) (revision v0.2)"; 5.x says "Chip type: …". Accept both
    # so this keeps working whether it runs against the repo venv or the IDF's own env.
    chip = re.search(r"Chip (?:is|type:)\s*(.+?)\s*$", txt, re.M)
    detected = re.search(r"Detecting chip type\.\.\.\s*(\S+)", txt)
    base = re.search(r"BASE MAC:\s*([0-9a-fA-F:]{17})", txt)
    mac = re.search(r"^MAC:\s*([0-9a-fA-F:]{17})\s*$", txt, re.M)
    chosen = (base or mac)
    if not chosen:
        raise FlashError(f"could not read a MAC from {port} — is a board attached and in bootloader?")
    family = (detected.group(1) if detected else "").lower().replace("-", "")
    return {
        "port": port,
        "chip": chip.group(1).strip() if chip else None,
        "target": family if family in TARGETS else None,
        "target_raw": family or None,
        "mac": chosen.group(1).upper(),
    }


def known_node_for_mac(mac: str, manifests: list[Path] | None = None) -> dict | None:
    """The manifest entry whose `mac` matches, or None. Tells the caller whether this board is already
    somebody — the difference between provisioning new hardware and re-flashing a live node."""
    import yaml
    mans = manifests or [REPO / "edge" / d / "nodes.yaml" for d in
                         ("esp32c6", "esp32s3-eth", "esp32c3")]
    for m in mans:
        if not m.exists():
            continue
        for node, info in ((yaml.safe_load(m.read_text()) or {}).get("nodes") or {}).items():
            if str((info or {}).get("mac", "")).upper() == mac.upper():
                return {"node_id": node, "manifest": str(m.relative_to(REPO)), **(info or {})}
    return None


# ── NVS blob ─────────────────────────────────────────────────────────────────────────────────────────

def build_nvs_blob(spec: dict, out_path: Path) -> Path:
    """Mint the per-node NVS blob. `spec` keys: node_id, mac (bind), broker_uri, wifi_ssid, wifi_psk,
    ntp_server, ota_host, gas_sensor.

    Deliberately does NOT carry cmd_secret. The secret is node-born (ADR-0036 L0) — the node mints it
    after its radio is up and hands it over once, at intake. Putting one here would reintroduce exactly
    the exposure that design removed, and would mean this code path handles secrets at all.
    """
    node_id = str(spec.get("node_id", "")).strip()
    if not NODE_RE.match(node_id):
        raise FlashError("node_id must be a slug [a-z0-9_]")
    mac = str(spec.get("mac", "")).strip().upper()
    if not MAC_RE.match(mac):
        raise FlashError(f"bind mac {mac!r} is not AA:BB:CC:DD:EE:FF")
    gas = str(spec.get("gas_sensor", "auto") or "auto")
    if gas not in GAS_CHOICES:
        raise FlashError(f"gas_sensor must be one of {', '.join(GAS_CHOICES)}")
    broker = str(spec.get("broker_uri", "")).strip()
    if not broker.startswith("mqtt://"):
        raise FlashError("broker_uri must be mqtt://host:port — a node with no broker is inert")

    rows = [
        ("node_id", node_id),
        ("broker_uri", broker),
        ("ntp_server", str(spec.get("ntp_server") or "pool.ntp.org")),
        ("ota_host", str(spec.get("ota_host") or "")),
        ("gas_sensor", gas),
        ("bind_mac", mac),
    ]
    for k in ("wifi_ssid", "wifi_psk"):           # optional: a wired S3 needs neither
        v = spec.get(k)
        if v:
            rows.append((k, str(v)))
    # A CSV field containing a comma or quote would silently corrupt the blob; refuse rather than mangle.
    for k, v in rows:
        if "," in v or '"' in v or "\n" in v:
            raise FlashError(f"{k} contains a character that cannot go in the NVS CSV (, \" or newline)")

    csv = "key,type,encoding,value\nha,namespace,,\n" + "".join(
        f"{k},data,string,{v}\n" for k, v in rows)

    if not NVS_GEN.exists():
        raise FlashError(f"nvs_partition_gen.py not found at {NVS_GEN} — set IDF_PATH")
    if not IDF_PYTHON.exists():
        raise FlashError(f"IDF python env not found at {IDF_PYTHON} — set HA_IDF_PYTHON")
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
        f.write(csv)
        csv_path = Path(f.name)
    try:
        _run([str(IDF_PYTHON), str(NVS_GEN), "generate", str(csv_path), str(out_path), hex(NVS_SIZE)],
             timeout=120)
    finally:
        csv_path.unlink(missing_ok=True)          # the CSV holds the wifi PSK — never leave it on disk
    if not out_path.exists() or out_path.stat().st_size != NVS_SIZE:
        raise FlashError(f"NVS blob generation produced {out_path.stat().st_size if out_path.exists() else 0}"
                         f" bytes, expected {NVS_SIZE}")
    return out_path


# ── image resolution ─────────────────────────────────────────────────────────────────────────────────

def image_set(target: str) -> dict:
    """The four artifacts a full flash writes, plus the app's app_desc version — verified to be a GENERIC
    image for this target. Refusing a per-node image here is the point: flashing `cbed_c6@dev` onto a
    board while telling NVS it is `hbed_c6` would produce a node whose binary and identity disagree."""
    t = TARGETS.get(target)
    if not t:
        raise FlashError(f"unknown target {target!r} (known: {', '.join(TARGETS)})")
    b = REPO / t["dir"] / "build"
    parts = {
        "0x0": b / "bootloader" / "bootloader.bin",
        "0x8000": b / "partition_table" / "partition-table.bin",
        "0xd000": b / "ota_data_initial.bin",
        "0x10000": b / t["app"],
    }
    missing = [str(p.relative_to(REPO)) for p in parts.values() if not p.exists()]
    if missing:
        raise FlashError(f"missing build artifacts for {target}: {', '.join(missing)} — build it first")

    import sys
    sys.path.insert(0, str(REPO / "tools"))
    from edge_ota import image_app_version           # single source of truth for reading app_desc
    ver = image_app_version(str(parts["0x10000"]))
    want = f"generic@{target}"
    if ver != want:
        raise FlashError(
            f"{t['app']} is branded '{ver}', not '{want}'. This flasher only writes GENERIC images — a "
            f"per-node build would give the board a binary identity that disagrees with its NVS blob. "
            f"Rebuild with version.txt = '{want}' and a secrets.h carrying no node identity.")
    return {"parts": {k: str(v) for k, v in parts.items()}, "version": ver,
            "app": str(parts["0x10000"].relative_to(REPO))}


def survey(*, node_secrets_path=None, master=None) -> dict:
    """Everything the flash UI needs in one call: what is plugged in, who it already is, and what we can
    write to it.

    Deliberately one round trip and deliberately read-only. The board is identified from the silicon, so
    the operator is confirming facts rather than typing them — chip family, revision and MAC all come from
    the chip itself, and `enrolled_as` says whether flashing it would orphan a node the dictator can
    currently command.
    """
    boards = []
    for p in list_ports():
        row = {"port": p["port"], "id": p["id"], "detected": False, "error": None}
        try:
            row.update(detect(p["port"]), detected=True)
        except FlashError as e:
            row["error"] = str(e)
            row["mac"] = p.get("mac_hint")           # by-id often still tells us who it is
        mac = row.get("mac")
        if mac:
            known = known_node_for_mac(mac)
            row["manifest_node"] = (known or {}).get("node_id")
            row["enrolled_as"] = None
            if node_secrets_path and master:
                try:
                    from server.control import secret_store as ss
                    for nid, rec in ss.load_lut(Path(node_secrets_path), master).items():
                        if str((rec or {}).get("mac", "")).upper() == mac.upper():
                            row["enrolled_as"] = nid
                            break
                except Exception as e:               # noqa: BLE001 — a bad LUT must not hide the board
                    row["lut_error"] = str(e)
            # The single most important thing for the operator to see before they click flash.
            row["needs_rotate"] = bool(row.get("enrolled_as"))
        boards.append(row)
    return {"boards": boards, "images": available_images(),
            "gas_choices": list(GAS_CHOICES),
            "defaults": provision_defaults(),
            "note": ("Chip family and MAC are read from the silicon; the gas sensor is probed by the "
                     "firmware at boot. Only node name, room and broker need choosing.")}


DEFAULTS_PATH = REPO / "instance" / "edge-provision.env"


def provision_defaults() -> dict:
    """Site defaults for provisioning, from `instance/edge-provision.env` (gitignored).

    Without this the operator retypes the Wi-Fi PSK on every flash, which is both tedious and a good way
    to strand a board on a typo'd network — a mistake that costs a re-cable to fix. The PSK is NEVER
    returned to the client; the UI shows that a stored one exists and the server fills it in at flash
    time. Keys: EDGE_WIFI_SSID, EDGE_WIFI_PSK, EDGE_BROKER_URI, EDGE_BROKER_URI_ALT, EDGE_OTA_HOST,
    EDGE_NTP_SERVER.
    """
    vals: dict[str, str] = {}
    if DEFAULTS_PATH.exists():
        for line in DEFAULTS_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            vals[k.strip()] = v.strip().strip("'\"")
    return {
        "wifi_ssid": vals.get("EDGE_WIFI_SSID", ""),
        "has_wifi_psk": bool(vals.get("EDGE_WIFI_PSK")),      # presence only — never the value
        "ntp_server": vals.get("EDGE_NTP_SERVER", "pool.ntp.org"),
        "ota_host": vals.get("EDGE_OTA_HOST", ""),
        "brokers": [b for b in (vals.get("EDGE_BROKER_URI", ""), vals.get("EDGE_BROKER_URI_ALT", "")) if b],
    }


def apply_defaults(spec: dict) -> dict:
    """Fill unset provisioning fields from the site defaults. Called server-side at flash time so the
    Wi-Fi PSK never has to leave the box."""
    d = provision_defaults()
    vals: dict[str, str] = {}
    if DEFAULTS_PATH.exists():
        for line in DEFAULTS_PATH.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                vals[k.strip()] = v.strip().strip("'\"")
    out = dict(spec)
    out.setdefault("wifi_ssid", d["wifi_ssid"])
    if not out.get("wifi_psk"):
        out["wifi_psk"] = vals.get("EDGE_WIFI_PSK", "")
    out.setdefault("ntp_server", d["ntp_server"])
    if not out.get("ota_host"):
        out["ota_host"] = d["ota_host"]
    if not out.get("broker_uri") and d["brokers"]:
        out["broker_uri"] = d["brokers"][0]
    return out


def available_images() -> list[dict]:
    """Which targets are actually flashable right now, with why-not for the ones that aren't.

    Phase 3 of the plan: the UI must never offer an image that does not exist, and when one is missing the
    operator needs the reason (not built / built per-node instead of generic) rather than a dead option.
    Reports the app hash so a flash can be traced back to an exact artifact after the fact.
    """
    import hashlib
    out = []
    for target in TARGETS:
        row = {"target": target, "ready": False, "reason": None, "version": None,
               "app": None, "sha256": None, "built": None}
        try:
            info = image_set(target)
            app = REPO / info["app"]
            row.update(ready=True, version=info["version"], app=info["app"],
                       sha256=hashlib.sha256(app.read_bytes()).hexdigest()[:16],
                       built=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(app.stat().st_mtime)))
        except FlashError as e:
            row["reason"] = str(e)
        out.append(row)
    return out


# ── the flash ────────────────────────────────────────────────────────────────────────────────────────

def flash_node(spec: dict, *, progress=None) -> dict:
    """Provision a board: detect → (rotate) → mint blob → erase → write → verify. Blocking; ~40s.

    `spec`: port, node_id, target?, gas_sensor?, broker_uri, wifi_ssid?, wifi_psk?, ntp_server?, ota_host?,
    confirm_rotate? (required to re-flash a board whose MAC is already enrolled), erase? (default True),
    node_secrets_path?/master? (needed only for rotation).
    """
    say = progress or (lambda m: log.info("%s", m))
    port = str(spec.get("port") or "")
    if not port.startswith("/dev/"):
        raise FlashError("port must be a /dev/tty* path")

    spec = apply_defaults(spec)          # site Wi-Fi/broker/NTP, so the PSK never crosses the wire
    with _PortLock():
        say(f"detecting board on {port}…")
        info = detect(port)
        target = str(spec.get("target") or info["target"] or "")
        if not target:
            raise FlashError(f"could not map chip {info['target_raw']!r} to a known target "
                             f"({', '.join(TARGETS)}) — pass target explicitly")
        mac = info["mac"]
        say(f"found {info['chip']} target={target} mac={mac}")

        images = image_set(target)                 # fail before touching the board if artifacts are wrong

        # Is this board already somebody? Two independent sources: the build manifest (what we intended)
        # and the encrypted LUT (what is actually enrolled). The LUT is authoritative for "can the
        # dictator command it", which is what a reflash breaks.
        known = known_node_for_mac(mac)
        enrolled_as = None
        lut_path = spec.get("node_secrets_path")
        master = spec.get("master")
        if lut_path and master:
            from server.control import secret_store as ss
            lut = ss.load_lut(Path(lut_path), master)
            for nid, rec in lut.items():
                if str((rec or {}).get("mac", "")).upper() == mac.upper():
                    enrolled_as = nid
                    break

        rotated = None
        if enrolled_as:
            if not spec.get("confirm_rotate"):
                raise FlashError(
                    f"this board is already enrolled as '{enrolled_as}'. Re-flashing wipes its NVS, so it "
                    f"will mint a NEW node-born secret while the dictator still holds the old one — the "
                    f"TOFU-lock will refuse the re-claim and the node ends up un-commandable. Re-submit "
                    f"with confirm_rotate=true to drop '{enrolled_as}' from the LUT so it can be claimed "
                    f"again. (You are holding the cable, which is the physical-presence trust root — but "
                    f"this is deliberate, not silent.)")
            from server.control import secret_store as ss
            p = Path(lut_path)
            lut = ss.load_lut(p, master)
            bak = p.with_suffix(p.suffix + ".bak")
            if p.exists():
                bak.write_bytes(p.read_bytes())
            lut.pop(enrolled_as, None)
            ss.save_lut(p, master, lut)
            if enrolled_as in ss.load_lut(p, master):          # verify, roll back on failure
                p.write_bytes(bak.read_bytes())
                raise FlashError("rotation failed round-trip verification; LUT rolled back, nothing flashed")
            rotated = enrolled_as
            say(f"rotated: dropped '{enrolled_as}' from the LUT (it can now be claimed fresh)")

        node_id = str(spec.get("node_id") or "")
        say(f"minting NVS blob for {node_id} bound to {mac}…")
        tmpdir = Path(tempfile.mkdtemp(prefix="ha-flash-"))
        try:
            blob = build_nvs_blob({**spec, "mac": mac}, tmpdir / "nvs.bin")

            if spec.get("erase", True):
                say("erasing flash (guarantees a virgin NVS — no stale keys or claim latch)…")
                _esptool("erase_flash", port=port, timeout=180)

            say(f"writing generic image ({images['version']}) + NVS blob…")
            args = ["--chip", target, "-b", "460800", "--before", "default_reset", "--after", "hard_reset",
                    "write_flash", "--flash_mode", "dio", "--flash_size", "4MB", "--flash_freq", "80m"]
            for off, path in images["parts"].items():
                args += [off, path]
            args += [NVS_OFFSET, str(blob)]
            out = _esptool(*args, port=port, timeout=420)
            verified = out.count("Hash of data verified.")
            if verified < len(images["parts"]) + 1:
                raise FlashError(f"only {verified} of {len(images['parts']) + 1} regions verified — "
                                 f"treat this board as half-written and re-flash it")
            say(f"flash complete — {verified} regions hash-verified; board reset")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)   # the blob is short-lived by design

    return {"status": "flashed", "node_id": node_id, "mac": mac, "target": target,
            "image": images["app"], "image_version": images["version"],
            "rotated_from": rotated,
            "previously_known_as": (known or {}).get("node_id"),
            "next": "the node boots, mints its own secret, and announces hello — adopt it in the PWA"}
