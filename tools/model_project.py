#!/usr/bin/env python3
"""model_project.py — project the live registries onto the ADR-0034 object model.

The **descriptive acceptance test** for the Node / Ability / Entity object model
([docs/DEVICE-MODEL.md](../docs/DEVICE-MODEL.md), ADR-0034): force every registered device through
`Node (transport class) → Ability (CONFORMANCE A–K) → Entity (device_id + area)`. A clean decomposition
with **no UNEXPLAINED leftover** = the model holds.

The distinction this tool enforces:
  • **MODEL LEAK** (exit 2) — a device whose Node-class or Ability can't be named at all. An *unmodeled* thing;
    the only real failure.
  • **stale / orphan** (expected) — a registered Entity with no fresh data, or fresh data with no registry.
    NOT a model failure: the model *has* a bucket for it. Liveness is an attribute of an entity's originating
    **sense** ability — a pure actuator (traits only) is never "stale".

Read-only. Defaults to this checkout's `instance/`; pass `--instance` for another (do NOT point it at a
live production-data-bearing standby — read `docs/DEVICE-MODEL.md` and the airgap notes first).

  tools/model_project.py                 # human table + verdict, exit 0/2
  tools/model_project.py --json          # machine-readable report
"""
from __future__ import annotations
import argparse, json, sqlite3, sys, time
from collections import Counter
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
NODE_CLASSES = {"mqtt-broker", "edge-signed", "ble-passive", "local-driver"}
FRESH_S = 1800
# control device_types with an onboard sensor that is ingested non-authoritatively (CONFORMANCE ability A,
# authoritative=0): the appliance reports e.g. its own RH for display but it is never a control source.
ONBOARD_SENSE = {"dehumidifier", "air_purifier"}


def _load(p: Path) -> dict:
    return (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}


def _senses(dtype: str) -> list[str]:
    t = (dtype or "").lower()
    if any(k in t for k in ("gas", "bme680", "sgp")):
        return ["A:senses-gas"]
    if any(k in t for k in ("meter", "switchbot", "aranet", "radon")):
        return ["A:senses"]
    if any(k in t for k in ("energy", "plug", "power")):
        return ["A:senses-energy"]
    return [f"A:senses({t})"] if t else []


def _ages(hot_db: Path) -> dict[str, float]:
    ages: dict[str, float] = {}
    if not hot_db.exists():
        return ages
    try:
        con = sqlite3.connect(f"file:{hot_db}?mode=ro", uri=True)   # read-only, cannot write
        now = time.time()
        for did, ts in con.execute("SELECT device_id, last_ts FROM device_last_seen"):
            try:
                ages[did] = now - time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
            except Exception:
                pass
        con.close()
    except Exception as e:  # noqa: BLE001
        print(f"# hot.db read skipped: {e}", file=sys.stderr)
    return ages


def project(inst: Path, hot_db: Path) -> tuple[list[dict], list[dict], dict]:
    tas = _load(inst / "tasmota-devices.yaml")
    lev = _load(inst / "levoit-devices.yaml")
    dev = (_load(inst / "devices.yaml").get("devices") or {})
    ctrl = (_load(inst / "control.yaml").get("devices") or {})
    ages = _ages(hot_db)
    entities: list[dict] = []
    misfits: list[dict] = []

    def add(device_id, node, node_class, abilities, area, address, registry, note=""):
        age = ages.get(device_id)
        senses = any(a.startswith("A") for a in abilities)
        fresh = age is not None and age < FRESH_S
        e = {"device_id": device_id, "node": node, "node_class": node_class, "abilities": abilities,
             "area": area, "address": address, "registry": registry, "age_s": (round(age) if age is not None else None),
             "senses": senses, "fresh": fresh, "note": note}
        entities.append(e)
        # MODEL LEAKS — the only real failures (an unmodeled thing)
        if node_class not in NODE_CLASSES:
            misfits.append({"kind": "LEAK:unnamed_node_class", "device_id": device_id, "detail": node_class})
        if not abilities:
            misfits.append({"kind": "LEAK:no_ability", "device_id": device_id, "detail": f"device_type didn't map to any A–K ability"})
        # EXPECTED buckets — only meaningful for sense-bearing entities (a pure actuator has no telemetry)
        elif senses:
            if age is None:
                misfits.append({"kind": "stale:never_logged", "device_id": device_id, "detail": f"registered in {registry}, no device_last_seen"})
            elif not fresh:
                misfits.append({"kind": "stale:silent", "device_id": device_id, "detail": f"age {round(age)}s > {FRESH_S}s"})
        return e

    # 1. Tasmota → mqtt-broker
    for topic, v in tas.items():
        if isinstance(v, dict):
            add(v.get("device_id", topic), topic, "mqtt-broker", _senses(v.get("device_type", "energy")),
                v.get("area"), topic, "tasmota-devices.yaml")

    # 2. devices.yaml → edge-signed (gas/relay nodes) or ble-passive (BLE meters)
    for key, v in dev.items():
        if not isinstance(v, dict):
            continue
        did = v.get("device_id") or v.get("id")
        node_id, dtype = v.get("node_id"), v.get("device_type", "")
        is_edge = bool(node_id) or any(k in dtype.lower() for k in ("gas", "bme", "sgp"))
        if is_edge:
            add(did, node_id or key, "edge-signed", _senses(dtype) + ["D:relays-BLE"], v.get("area"),
                str(key), "devices.yaml", note=(f"node_id={node_id}" if node_id else ""))
        else:
            add(did, key, "ble-passive", _senses(dtype), v.get("area"), str(key), "devices.yaml")

    # 3. control.yaml actuators (node: server → local-driver), Levoit sense fused in (dual-role)
    lev_ids = {v.get("device_id"): name for name, v in lev.items() if isinstance(v, dict)}
    for did, v in ctrl.items():
        if not isinstance(v, dict):
            continue
        node, traits = v.get("node", "?"), list((v.get("traits") or {}).keys())
        ab = []
        if did in lev_ids or (v.get("device_type") in ONBOARD_SENSE):
            ab.append("A:senses(onboard,non-auth)")   # refinement #3: appliance's own sensor, ingested auth=0
        if traits:
            ab.append("B:actuator-traits(" + ",".join(traits) + ")")
        reg = "levoit-devices.yaml+control.yaml" if did in lev_ids else "control.yaml"
        addr = lev_ids.get(did, did)
        node_class = "local-driver" if node == "server" else "edge-signed"
        add(did, did if node == "server" else node, node_class, ab, v.get("area"), addr, reg, note=f"node:{node}")

    # 4. orphans — fresh device_last_seen with no registry (the live-but-unregistered signature)
    reg_ids = {e["device_id"] for e in entities}
    for did, age in ages.items():
        if did not in reg_ids and age < FRESH_S:
            misfits.append({"kind": "orphan:live_unregistered", "device_id": did, "detail": f"fresh ({round(age)}s) but in no registry"})

    # aliases — one device_id surfaced by >1 node (many-to-many; benign, but stale aliases are cleanup bait)
    by_id = Counter(e["device_id"] for e in entities)
    aliases = {d: [e["node"] for e in entities if e["device_id"] == d] for d, n in by_id.items() if n > 1}

    leaks = [m for m in misfits if m["kind"].startswith("LEAK")]
    summary = {
        "instance": str(inst), "entities": len(entities),
        "node_classes": dict(Counter(e["node_class"] for e in entities)),
        "ability_kinds": dict(Counter(a.split(":")[0] for e in entities for a in e["abilities"])),
        "misfits_by_kind": dict(Counter(m["kind"].split(":")[0] for m in misfits)),
        "aliases": aliases,
        "MODEL_LEAKS": len(leaks),
        "VERDICT": "MODEL HOLDS (0 unmodeled leftovers)" if not leaks else "MODEL LEAK — unmodeled device(s)",
    }
    return entities, misfits, summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Project live registries onto the ADR-0034 Node/Ability/Entity model")
    ap.add_argument("--instance", type=Path, default=REPO / "instance")
    ap.add_argument("--hot-db", type=Path, default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    hot = a.hot_db or (a.instance / "db" / "hot.db")
    entities, misfits, summary = project(a.instance, hot)
    if a.json:
        print(json.dumps({"summary": summary, "entities": entities, "misfits": misfits}, indent=2))
    else:
        print(f"\n=== PROJECTION: {a.instance}  (hot.db: {'yes' if hot.exists() else 'none'}) ===")
        for e in entities:
            f = "fresh" if e["fresh"] else (f"STALE {e['age_s']}s" if e["age_s"] is not None else ("—" if not e["senses"] else "no-data"))
            print(f"  {e['device_id']:26} node={e['node']:20} [{e['node_class']:12}] {f:12} {' '.join(e['abilities'])}")
        if summary["aliases"]:
            print("\n--- aliases (one entity, multiple nodes — benign; retire stale ones) ---")
            for d, nodes in summary["aliases"].items():
                print(f"  {d:26} nodes: {', '.join(nodes)}")
        print("\n--- misfits (LEAK=model failure · stale/orphan=named bucket, expected) ---")
        for m in misfits:
            print(f"  {m['kind']:28} {m['device_id']:26} {m['detail']}")
        print("\n--- summary ---")
        print(json.dumps(summary, indent=2))
    return 0 if summary["MODEL_LEAKS"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
