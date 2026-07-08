#!/usr/bin/env python3
"""device_descriptor.py — serialize a device's identity + registry + control-plane + recent history to
portable JSON, for cross-network migration (Phase 3). PURE READ, no side effects.

The idempotent APPLY side lives in ha-2's POST /api/v1/admin/devices:import (server/api/control.py),
which reuses device_migrate's per-store logic. This serializer reuses device_migrate's exact store lists
(CONTROL_TABLES, the registry set) so the descriptor is complete by construction.

Note on secrets: the per-device HMAC secret (control_secrets.yaml) is NOT emitted in cleartext — the
descriptor records only its PRESENCE. In the air-gap migration ha-2 already holds it (provision-peer sync
of the dictator-files manifest); for a greenfield target it is prepositioned out-of-band.
See docs/airgap/MIGRATION-DESIGN-LOG.md (Phase 3, gotcha 7).
"""
import argparse, json, sqlite3, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server.maintenance.device_migrate import REPO_ROOT, CONTROL_TABLES, _registry_paths  # noqa: E402

CONTROL_DB = REPO_ROOT / "instance/db/control.db"
HOT_DB = REPO_ROOT / "instance/db/hot.db"
SECRETS = REPO_ROOT / "instance/control_secrets.yaml"


def _ro(db):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def _sqlite_rows(db, tables, device_id):
    out = {}
    if not Path(db).exists():
        return out
    con = _ro(db); con.row_factory = sqlite3.Row
    for t in tables:
        try:
            rows = [dict(r) for r in con.execute(f"SELECT * FROM {t} WHERE device_id=?", (device_id,))]
            if rows:
                out[t] = rows
        except sqlite3.OperationalError:
            pass  # table missing or no device_id column
    con.close()
    return out


def _scan_level(container, device_id):
    """entries keyed BY device_id (control.yaml) OR values carrying a device_id: field
    (tasmota-devices.yaml topic-keyed, devices.yaml MAC-keyed, ...)."""
    if not isinstance(container, dict):
        return {}
    return {k: v for k, v in container.items()
            if k == device_id or (isinstance(v, dict) and v.get("device_id") == device_id)}


def _registry_entry(device_id):
    import yaml
    found = {}
    for p in _registry_paths():
        try:
            data = yaml.safe_load(p.read_text()) or {}
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        hits = _scan_level(data, device_id)
        # also descend ONE level into container keys (e.g. devices.yaml -> {devices: {mac: {...}}})
        for k, v in data.items():
            if isinstance(v, dict) and "device_id" not in v:
                hits.update({f"{k}.{ik}": iv for ik, iv in _scan_level(v, device_id).items()})
        if hits:
            found[p.name] = hits
    return found


def _secret_present(device_id):
    if not SECRETS.exists():
        return False
    import yaml
    try:
        return device_id in (yaml.safe_load(SECRETS.read_text()) or {})
    except Exception:
        return False


def _history(device_id, since_ts):
    out = {}
    if not Path(HOT_DB).exists():
        return out
    con = _ro(HOT_DB); con.row_factory = sqlite3.Row
    try:
        out["readings"] = [dict(r) for r in con.execute(
            "SELECT ts,metric,value,unit,device_type,area,transport,authoritative FROM readings "
            "WHERE device_id=? AND ts>=? ORDER BY ts", (device_id, since_ts))]
    except sqlite3.OperationalError:
        pass
    try:
        out["device_last_seen"] = [dict(r) for r in con.execute(
            "SELECT * FROM device_last_seen WHERE device_id=?", (device_id,))]
    except sqlite3.OperationalError:
        pass
    con.close()
    return out


def serialize(device_id, history_hours=48):
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - history_hours * 3600))
    return {
        "schema": 1,
        "device_id": device_id,
        "registry": _registry_entry(device_id),
        "control": _sqlite_rows(CONTROL_DB, CONTROL_TABLES, device_id),
        "secret_present": _secret_present(device_id),
        "history_since": since,
        "history": _history(device_id, since),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("device_id")
    ap.add_argument("--history-hours", type=int, default=48)
    ap.add_argument("--summary", action="store_true", help="print a compact summary instead of full JSON")
    a = ap.parse_args()
    d = serialize(a.device_id, a.history_hours)
    if a.summary:
        reg = ", ".join(f"{f}:{list(h)}" for f, h in d["registry"].items()) or "none"
        ctl = {t: len(rows) for t, rows in d["control"].items()}
        print(f"device_id   : {d['device_id']}")
        print(f"registry    : {reg}")
        print(f"control rows: {ctl or 'none'}")
        print(f"secret      : {'present' if d['secret_present'] else 'absent'}")
        print(f"history     : {len(d['history'].get('readings', []))} readings since {d['history_since']}, "
              f"{len(d['history'].get('device_last_seen', []))} last-seen")
    else:
        print(json.dumps(d, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
