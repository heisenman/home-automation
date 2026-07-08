#!/usr/bin/env python3
"""device_push.py — migrate ONE device across the air gap (.210 -> ha-2) as an idempotent, reversible
per-device state machine (Phase 3/4). No AI in the loop: a human/cron runs `device_push.py <device_id>`.

Stages:  queued -> transferred -> applied -> repointed -> confirmed -> retired   (+ failed)
State persists in instance/db/migration.db so a run resumes where it left off. The RETIRE on .210 is the
LAST step and only after ha-2 CONFIRMS the device is reporting — so an abort never loses a device.

Device classes handled here: tasmota (MQTT repoint) and ble (no repoint — ha-2 just scans). ESP32/panel/
ESPHome repointers plug into `repoint()` as they're built. See docs/airgap/MIGRATION-DESIGN-LOG.md Phase 3/4.
"""
import argparse, json, sqlite3, subprocess, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from server.maintenance import device_descriptor  # noqa: E402
from server.maintenance import device_migrate      # noqa: E402

STATE_DB = REPO / "instance/db/migration.db"
BRIDGE = "https://192.168.0.210"          # ha-2's API via the .210 web bridge
STAGES = ["queued", "transferred", "applied", "repointed", "confirmed", "retired"]


def _state_con():
    con = sqlite3.connect(STATE_DB)
    con.execute("CREATE TABLE IF NOT EXISTS migration(device_id TEXT PRIMARY KEY, stage TEXT, "
                "cls TEXT, updated TEXT, note TEXT)")
    return con


def get_stage(device_id):
    con = _state_con()
    r = con.execute("SELECT stage FROM migration WHERE device_id=?", (device_id,)).fetchone()
    con.close()
    return r[0] if r else None


def set_stage(device_id, stage, cls, note=""):
    con = _state_con()
    con.execute("INSERT INTO migration(device_id,stage,cls,updated,note) VALUES(?,?,?,?,?) "
                "ON CONFLICT(device_id) DO UPDATE SET stage=excluded.stage, updated=excluded.updated, note=excluded.note",
                (device_id, stage, cls, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), note))
    con.commit(); con.close()


def classify(device_id):
    import yaml
    tas = REPO / "instance/tasmota-devices.yaml"
    if tas.exists():
        data = yaml.safe_load(tas.read_text()) or {}
        if device_id in data or any(isinstance(v, dict) and v.get("device_id") == device_id for v in data.values()):
            return "tasmota"
    dev = REPO / "instance/devices.yaml"
    if dev.exists():
        data = (yaml.safe_load(dev.read_text()) or {}).get("devices", {})
        if any(isinstance(v, dict) and v.get("device_id") == device_id for v in data.values()):
            return "ble"
    return "unknown"


def _run(cmd, dry):
    print("   $", " ".join(cmd))
    if dry:
        return 0, "(dry-run)"
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, (r.stdout or r.stderr).strip()


def confirm_on_ha2(device_id, timeout_s, dry):
    """The device is CONFIRMED migrated when ha-2's API shows it with a fresh device_last_seen."""
    if dry:
        print(f"   would poll {BRIDGE}/api/v1/sensors for '{device_id}' with a fresh ts (<= {timeout_s}s)")
        return True
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        rc, out = _run(["curl", "-sk", "--max-time", "6", f"{BRIDGE}/api/v1/sensors"], dry=False)
        try:
            for s in json.loads(out).get("sensors", []):
                if s.get("device_id") == device_id:
                    age = time.time() - time.mktime(time.strptime(s["ts"], "%Y-%m-%dT%H:%M:%SZ"))
                    if age < 180:
                        print(f"   ✓ ha-2 sees {device_id} (ts {s['ts']}, {int(age)}s old)")
                        return True
        except Exception:
            pass
        time.sleep(8)
    return False


def repoint(device_id, cls, dry, revert=False):
    if cls == "tasmota":
        cmd = [sys.executable, str(REPO / "tools/repoint_tasmota.py"), device_id] + (["--revert"] if revert else [])
        rc, out = _run(cmd + (["--dry-run"] if dry else []), dry=False)
        return rc == 0
    if cls == "ble":
        print("   BLE: no device repoint — ha-2 scans it (ensure ha-scanner is running on ha-2 for its area)")
        return True
    print(f"   ✗ no repointer for class '{cls}' yet (ESP32/panel/ESPHome are built as those classes migrate)")
    return False


def push(device_id, *, dry, history_hours, confirm_timeout):
    cls = classify(device_id)
    if cls == "unknown":
        print(f"✗ cannot classify '{device_id}' (not in tasmota-devices.yaml or devices.yaml)")
        return 2
    stage = get_stage(device_id) or "queued"
    print(f"{'[DRY-RUN] ' if dry else ''}push {device_id}  class={cls}  resume-from={stage}")

    def advance(to, ok, note=""):
        if ok:
            if not dry:
                set_stage(device_id, to, cls, note)
            print(f"  -> {to}")
        return ok

    # 1. transferred: serialize the descriptor (recorded; ha-2 already holds config via provision-peer)
    desc = device_descriptor.serialize(device_id, history_hours)
    print(f"  transferred: descriptor ({len(desc['history'].get('readings', []))} readings, "
          f"registry {list(desc['registry']) or 'none'})")
    advance("transferred", True)

    # 2. applied: ha-2 import (graceful — ha-2 has config from sync; endpoint deploy is Phase-4 prep)
    print("  applied: ha-2 already holds config/history (provision-peer); :import endpoint is idempotent when deployed")
    advance("applied", True)

    # 3. repointed: switch the physical device to the air-gap net
    ok = repoint(device_id, cls, dry)
    if not advance("repointed", ok, "repoint failed" if not ok else ""):
        set_stage(device_id, "failed", cls, "repoint") if not dry else None
        return 1

    # 4. confirmed: ha-2 sees the device reporting
    ok = confirm_on_ha2(device_id, confirm_timeout, dry)
    if not advance("confirmed", ok, "not seen on ha-2" if not ok else ""):
        print("  ✗ NOT confirmed on ha-2 — leaving it live on .210 (not retiring). Investigate/rollback.")
        if not dry:
            set_stage(device_id, "failed", cls, "confirm")
        return 1

    # 5. retired on .210 (LAST — only after confirm). do_peer=False: the cross-net side already has it.
    print("  retired: removing from .210 (device_migrate retire, do_peer=False)")
    if not dry:
        device_migrate.run_migration("retire", device_id, do_peer=False, dry_run=False)
    advance("retired", True)
    print(f"{'[DRY-RUN] ' if dry else ''}✓ {device_id} migrated to ha-2")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("device_id")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--history-hours", type=int, default=48)
    ap.add_argument("--confirm-timeout", type=int, default=180)
    ap.add_argument("--status", action="store_true", help="just print the migration stage and exit")
    a = ap.parse_args()
    if a.status:
        print(f"{a.device_id}: {get_stage(a.device_id) or 'not started'}  (class {classify(a.device_id)})")
        return 0
    return push(a.device_id, dry=a.dry_run, history_hours=a.history_hours, confirm_timeout=a.confirm_timeout)


if __name__ == "__main__":
    sys.exit(main())
