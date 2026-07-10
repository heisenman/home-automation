#!/usr/bin/env python3
"""device_push.py — migrate ONE device across the air gap (.210 -> ha-2) as an idempotent, reversible
per-device state machine (Phase 3/4). No AI in the loop: a human/cron runs `device_push.py <device_id>`.

Stages:  queued -> transferred -> applied -> repointed -> confirmed -> retired   (+ failed)
State persists in instance/db/migration.db so a run resumes where it left off. The RETIRE on .210 is the
LAST step and only after ha-2 CONFIRMS the device is reporting — so an abort never loses a device.

`classify()` is a **Node / transport-plane** classifier (ADR-0034): it answers *what repoint/migration
procedure this physical unit needs* — NOT what the device is (that's its Abilities; see docs/DEVICE-MODEL.md).
Node classes: mqtt-broker · edge-signed · ble-passive · local-driver (+ unknown). See docs/airgap/
MIGRATION-DESIGN-LOG.md Phase 3/4.
"""
import argparse, json, os, shlex, sqlite3, subprocess, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from server.maintenance import device_descriptor  # noqa: E402
from server.maintenance import device_migrate      # noqa: E402

STATE_DB = REPO / "instance/db/migration.db"
BRIDGE = "https://192.168.0.210"          # ha-2's API via the .210 web bridge
BROKER_HOST = os.environ.get("HA_BROKER_HOST", "localhost")   # .210's broker (where retained ghosts live)
BROKER_PORT = int(os.environ.get("HA_BROKER_PORT", "1883"))
STAGES = ["queued", "transferred", "applied", "repointed", "confirmed", "retired"]
# Node/transport-plane classes (ADR-0034) — the repoint procedure a physical unit needs, not what it IS.
NODE_CLASSES = ("mqtt-broker", "edge-signed", "ble-passive", "local-driver")
PENDING_HOURS = 6                         # grace window: hold the migrated device quiet, then the sweeper drops it


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


def _node_manifests(edge_dir=None):
    """The ESP32 edge-node identity manifests (ADR-0020): edge/*/nodes.yaml. Keys = node_ids."""
    return sorted((Path(edge_dir) if edge_dir else REPO / "edge").glob("*/nodes.yaml"))


def _current_node_ids(edge_dir=None):
    """Set of live edge node_ids across all edge/*/nodes.yaml manifests."""
    import yaml
    ids = set()
    for man in _node_manifests(edge_dir):
        try:
            doc = yaml.safe_load(man.read_text()) or {}
        except Exception:
            continue
        nodes = doc.get("nodes", doc) if isinstance(doc, dict) else {}
        if isinstance(nodes, dict):
            ids |= set(nodes)
    return ids


def resolve_node_id(device_id, *, edge_dir=None, devices_path=None):
    """Map a registry device_id to its edge firmware node_id (the home/edge/<node>/cmd owner), or None.
    Two cases: (1) device_id IS a node_id — relay nodes, device_id==node_id; (2) the gas-node SPLIT where
    the registry device_id (e.g. gas_hbed) differs from the firmware node_id (hbed_c6), carried as a
    `node_id:` field on the devices.yaml record. When a device_id has DUPLICATE records (a retired node +
    its live replacement — e.g. c6-bench-gas vs coffice_c6-gas both device_id=gas_c_office), PREFER the
    node_id that is a current edge node in nodes.yaml, so a stale record can't win the repoint target."""
    import yaml
    node_ids = _current_node_ids(edge_dir)
    if device_id in node_ids:
        return device_id
    dev = Path(devices_path) if devices_path else REPO / "instance/devices.yaml"
    if dev.exists():
        data = (yaml.safe_load(dev.read_text()) or {}).get("devices", {})
        cands = [v.get("node_id") for v in data.values()
                 if isinstance(v, dict) and v.get("device_id") == device_id and v.get("node_id")]
        for nid in cands:
            if nid in node_ids:
                return nid
        if cands:
            return cands[0]   # mapped but manifest-absent (e.g. not yet enrolled) — surface, don't drop
    return None


def classify(device_id, *, edge_dir=None):
    """Node/transport-plane class (ADR-0034) = the repoint/migration procedure this physical unit needs.
    Returns a NODE_CLASSES value or "unknown" (in no registry):
      mqtt-broker  — rewrite the device's broker URI (Tasmota NVS / ESPHome)          [tasmota-devices.yaml]
      edge-signed  — signed `repoint` directive to home/edge/<node>/cmd (ADR-0010)    [edge/*/nodes.yaml]
      ble-passive  — no device repoint; the dictator scans it                         [devices.yaml BLE]
      local-driver — driver runs on the dictator; the physical move is a MANUAL app/flash step, then
                     confirm + pending-hold (LAN actuators, host)                      [control.yaml node: server]
    """
    import yaml
    # local-driver FIRST: a control.yaml actuator driven server-side (node: server) is authoritatively
    # local-driver, whatever side-registry it also sits in (e.g. Levoit is also in levoit-devices.yaml).
    ctrl = REPO / "instance/control.yaml"
    if ctrl.exists():
        v = ((yaml.safe_load(ctrl.read_text()) or {}).get("devices", {}) or {}).get(device_id)
        if isinstance(v, dict) and v.get("node") == "server":
            return "local-driver"
    tas = REPO / "instance/tasmota-devices.yaml"
    if tas.exists():
        data = yaml.safe_load(tas.read_text()) or {}
        if device_id in data or any(isinstance(v, dict) and v.get("device_id") == device_id for v in data.values()):
            return "mqtt-broker"
    # edge-signed: device_id is a node_id in edge/*/nodes.yaml (relay nodes), OR maps to one via a
    # devices.yaml `node_id:` field (the gas-node device_id<->node_id split, e.g. gas_hbed -> hbed_c6).
    if resolve_node_id(device_id, edge_dir=edge_dir):
        return "edge-signed"
    dev = REPO / "instance/devices.yaml"
    if dev.exists():
        data = (yaml.safe_load(dev.read_text()) or {}).get("devices", {})
        if any(isinstance(v, dict) and v.get("device_id") == device_id for v in data.values()):
            return "ble-passive"
    return "unknown"


def _airgap_target():
    """(ssid, psk, broker_uri, host) for the air-gap net. psk from instance/openwrt/airgap_router.env
    (WIFI_PSK) or $WIFI_PSK. host/broker match repoint_tasmota + the migrated PMs."""
    ssid, host = "autohome_airgap", "192.168.1.210"
    psk = os.environ.get("WIFI_PSK")
    env = REPO / "instance/openwrt/airgap_router.env"
    if not psk and env.exists():
        for line in env.read_text().splitlines():
            s = line.strip()
            if s.startswith("WIFI_PSK="):
                # Parse the RHS with SHELL semantics (honor quotes, drop inline `# comment`). A naive
                # split("=",1)[1].strip().strip('"') mangles a `WIFI_PSK="val"  # note` line — it leaves
                # the closing quote + comment (observed: a 61-char "psk" vs the real 19). This file is
                # shell-sourced elsewhere (airgap-bridge-up.sh), so match that.
                try:
                    toks = shlex.split(s.split("=", 1)[1], comments=True)
                    psk = toks[0] if toks else ""
                except ValueError:
                    psk = s.split("=", 1)[1].strip().strip('"')
                break
    return ssid, psk, f"mqtt://{host}:1883", host


def _tas_path(path=None):
    return Path(path) if path is not None else REPO / "instance/tasmota-devices.yaml"


def _tasmota_entry(device_id, path=None):
    """(topic, area) for a tasmota device_id from tasmota-devices.yaml, else (None, None).
    The yaml KEY is the Tasmota %topic%; the value may also carry an explicit device_id + area."""
    import yaml
    tas = _tas_path(path)
    if not tas.exists():
        return None, None
    data = yaml.safe_load(tas.read_text()) or {}
    for topic, entry in data.items():
        if topic == device_id or (isinstance(entry, dict) and entry.get("device_id") == device_id):
            return topic, (entry.get("area") if isinstance(entry, dict) else None)
    return None, None


def deregister_tasmota(device_id, *, dry_run=False, path=None):
    """Remove device_id's entry from tasmota-devices.yaml so classify() no longer resurrects it
    (and .245's reconcile can't revive it). Returns the removed topic key, or None if absent."""
    import yaml
    tas = _tas_path(path)
    if not tas.exists():
        return None
    data = yaml.safe_load(tas.read_text()) or {}
    key = next((t for t, e in data.items()
                if t == device_id or (isinstance(e, dict) and e.get("device_id") == device_id)), None)
    if key is not None and not dry_run:
        del data[key]
        tas.write_text(yaml.safe_dump(data, sort_keys=False))
    return key


def drop_cleanup(device_id, *, dry_run=False, broker=None, broker_port=None, tas_path=None):
    """ADR-0028 pending-DROP cleanup (folded here from the sweeper). Once a hold expires with no fresh
    data and the device is marked retired, this removes the ghost the retire would otherwise leave:
      • clear the lingering RETAINED MQTT — the server state topic AND the Tasmota LWT, and
      • DEREGISTER the device from tasmota-devices.yaml.
    History is untouched (data-storage-is-primary). Idempotent + best-effort. Returns a report dict."""
    broker = BROKER_HOST if broker is None else broker
    broker_port = BROKER_PORT if broker_port is None else broker_port
    topic, area = _tasmota_entry(device_id, path=tas_path)   # read BEFORE deregister removes the entry
    topics = ([f"home/{area}/{device_id}/state"] if area else []) + ([f"tele/{topic}/LWT"] if topic else [])
    return {
        "device_id": device_id,
        "retained_cleared": device_migrate.clear_retained(broker, broker_port, topics, dry_run=dry_run),
        "deregistered": deregister_tasmota(device_id, dry_run=dry_run, path=tas_path),
    }


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
    if cls == "mqtt-broker":
        cmd = [sys.executable, str(REPO / "tools/repoint_tasmota.py"), device_id] + (["--revert"] if revert else [])
        rc, out = _run(cmd + (["--dry-run"] if dry else []), dry=False)
        return rc == 0
    if cls == "edge-signed":
        return _repoint_esp32(device_id, dry, revert)
    if cls == "ble-passive":
        print("   ble-passive: no device repoint — ha-2 scans it (ensure ha-scanner is running on ha-2 for its area)")
        return True
    if cls == "local-driver":
        # LAN actuator / host driver (ADR-0034 — closes device-push-actuator-class). The driver already runs
        # on the destination dictator, so there is NO broker URI to rewrite: the physical network move is a
        # MANUAL re-provision (vendor app / ESPHome reflash) done out-of-band. Run device_push AFTER that move
        # so the next stages (confirm on ha-2 + pending-hold on .210) can see it. NEVER factory-reset a
        # Midea/Levoit — it rotates the LAN key/token (see midea-app-dep memory).
        if revert:
            print("   local-driver revert = manual re-provision back to the household net; not driven here")
            return False
        print("   local-driver: no network repoint — the physical move is MANUAL (re-provision the appliance")
        print("     onto the air-gap net via its app/ESPHome). Run this step AFTER that move so confirm sees it.")
        return True
    print(f"   ✗ no repointer for node class '{cls}' yet")
    return False


def _repoint_esp32(device_id, dry, revert):
    """Drive tools/repoint_node.py (signed `repoint` op) for a native-C esp32 node. Secrets (HA_CMD_SECRET
    per-node + WIFI_PSK) travel via ENV, never argv, so they never hit logs. repoint_node has no --dry-run,
    so in dry mode we print the command and don't send. Node revert is firmware-automatic (boot-count)."""
    if revert:
        print("   esp32 revert = firmware boot-count auto-revert (or re-run repoint_node with household "
              "params); not driven from device_push")
        return False
    if not os.environ.get("HA_CMD_SECRET"):
        print("   ✗ HA_CMD_SECRET not set — export the node's per-device command secret before migrating "
              "(resolve from node_secrets.enc via enroll_node; a human/cron supplies it)")
        return False
    node_id = resolve_node_id(device_id)
    if not node_id:
        print(f"   ✗ can't resolve a firmware node_id for '{device_id}' — add `node_id: <node>` to its "
              f"devices.yaml record (the home/edge/<node>/cmd owner)")
        return False
    if node_id != device_id:
        print(f"   device '{device_id}' -> firmware node '{node_id}'")
    ssid, psk, broker, host = _airgap_target()
    if not psk:
        print("   ✗ no Wi-Fi PSK — set $WIFI_PSK or instance/openwrt/airgap_router.env WIFI_PSK")
        return False
    cmd = [sys.executable, str(REPO / "tools/repoint_node.py"), "--node", node_id,
           "--ssid", ssid, "--broker", broker, "--ntp", host, "--ota-host", host,
           "--broker-host", "localhost", "--wait"]
    if dry:
        print("   $ (dry, NOT sent)", " ".join(cmd), "  [WIFI_PSK + HA_CMD_SECRET via env]")
        return True
    print("   $", " ".join(cmd), "  [secrets via env]")
    r = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, "WIFI_PSK": psk})
    print("   ", (r.stdout or r.stderr).strip())
    return r.returncode == 0


def push(device_id, *, dry, history_hours, confirm_timeout):
    cls = classify(device_id)
    if cls == "unknown":
        print(f"✗ cannot classify '{device_id}' (in no registry: control.yaml / tasmota-devices.yaml / "
              f"edge nodes.yaml / devices.yaml)")
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
        print(f"    diagnose WHY on ha-2: tools/migration_activate.py check {device_id}  "
              "(distinguishes not-registered [silent-drop] vs not-logging vs quarantined)")
        if not dry:
            set_stage(device_id, "failed", cls, "confirm")
        return 1

    # 5. PENDING-hold on .210 (LAST — only after confirm). NOT a hard retire: an immediate retire can
    #    half-fail and leave a stale ghost (readings/registry/LWT lingering, GUI complaints). Instead mark
    #    the device pending — hidden from the GUI + suppressed from alerts/ntfy NOW — and let the
    #    pending-sweeper drop it after the grace window UNLESS it reports fresh data (a failed migration
    #    self-heals instead of vanishing). History is kept (data-storage-is-primary).
    print(f"  pending: holding {device_id} quiet on .210 for {PENDING_HOURS}h (sweeper resolves it)")
    if not dry:
        import sqlite3
        from server.control import control_store as store
        cc = sqlite3.connect(str(REPO / "instance/db/control.db"))
        store.ensure_schema(cc)
        until = store.set_pending(cc, device_id, PENDING_HOURS); cc.close()
        print(f"    pending_until={until}")
    advance("retired", True)     # stage name kept for continuity; it's now a pending-hold, not a delete
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
