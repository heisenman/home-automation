#!/usr/bin/env python3
"""
Required-services resolver + supervisor. ONE tool, driven by the declarative manifest
`provisioning/required-services.yaml` — no hardcoded unit lists anywhere.

Consumers:
  install.sh           -> `required_services.py list --enable`   (which units this host must enable)
  ha-supervisor.timer  -> `required_services.py check`           (flag missing/inactive required units + alert)
  humans               -> `required_services.py check` / `list`  (audit a host)

This host's ROLES come from `instance/host-roles.yaml` (gitignored, per-box: `roles: [core, dictator]`), so the
box-local air-gap straddle stays out of the committed repo. The manifest defines the roles (universal).

`check` is the fix for the Levoit-missing-14h class: `is-enabled == not-found` means a required unit was NEVER
INSTALLED (the dangerous, silent case) — systemd can't warn about a unit it doesn't know should exist.
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("pyyaml required", file=sys.stderr); sys.exit(2)

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "provisioning" / "required-services.yaml"
HOST_ROLES = REPO / "instance" / "host-roles.yaml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def host_roles(explicit: list[str] | None) -> list[str]:
    if explicit:
        return explicit
    if HOST_ROLES.exists():
        r = (_load(HOST_ROLES) or {}).get("roles") or []
        if r:
            return list(r)
    raise SystemExit(f"no roles: pass --roles or create {HOST_ROLES} with `roles: [core, ...]`")


def host_options(path: Path | None = None) -> dict:
    """Per-box knobs from host-roles.yaml beyond the role list: `unit_prefix` and `exclude`.

    Exists for a SECOND stack co-resident on one box — .210 runs the household `ha-*` set AND the
    air-gap standby's `ha-ag-*` set from a separate checkout. Before this, that second set was
    hand-curated and therefore invisible to the supervisor, which is precisely how it ended up with a
    full ingest path and NO compactor (2026-07-31: hot.db grew to 2.9M rows, every O(rows) read scaled
    with it, and keepalived's probe crossed its timeout — failover silently disarmed for four days).
    A hand-kept list cannot be checked; a declared one can.

    `exclude` is how a replica says "deliberately not here", so a genuine omission still shows as a GAP
    while an intentional one is documented in-place rather than remembered.
    """
    p = path or HOST_ROLES
    d = _load(p) if p.exists() else {}
    return {"unit_prefix": d.get("unit_prefix") or "", "exclude": set(d.get("exclude") or [])}


def resolve(manifest: dict, roles: list[str], opts: dict | None = None) -> dict[str, dict]:
    """Union the units across this host's roles. A later role overrides `must` for the same unit
    (e.g. dictator sets ha-controller must=active, overriding core's `gated`).

    `opts` (see host_options) may rename `ha-<x>` to `<prefix><x>` and drop deliberately-absent units.
    Exclusion is matched on the MANIFEST name (`ha-scanner.service`), not the prefixed one, so the
    exclude list reads the same on every box.
    """
    opts = opts or {"unit_prefix": "", "exclude": set()}
    prefix, exclude = opts.get("unit_prefix") or "", opts.get("exclude") or set()
    defs = manifest.get("roles", {})
    out: dict[str, dict] = {}
    for role in roles:
        if role not in defs:
            raise SystemExit(f"unknown role {role!r} (manifest roles: {', '.join(defs)})")
        for u in defs[role].get("units", []):
            name = u["unit"]
            if name in exclude:
                continue
            if prefix and name.startswith("ha-"):
                name = prefix + name[len("ha-"):]
            # role order = precedence; a stronger 'must' (active) wins over 'gated'
            if name in out and out[name]["must"] == "active":
                continue
            out[name] = {"must": u.get("must", "active"), "source": u.get("source"),
                         "optional": bool(u.get("optional")), "on_fail": u.get("on_fail", "heal")}
    return out


def _sysctl(verb: str, unit: str) -> str:
    r = subprocess.run(["systemctl", verb, unit], capture_output=True, text=True)
    return (r.stdout or r.stderr).strip()


def unit_states(units: list[str]) -> dict[str, tuple[str, str]]:
    """{unit: (enabled_state, active_state)} for many units in ONE systemctl call.

    Was two `systemctl` subprocesses PER UNIT (`is-enabled` + `is-active`), i.e. 82 processes per
    supervisor run on .210's 41-unit household set — and this box now runs the check on two stacks. That
    is a lot of fork/exec for a status read, and fork traffic is exactly what board task os-idle-churn is
    about: it is not the CPU that matters so much as the wakeups (C-state residency).

    `systemctl show` accepts many units and emits one blank-line-separated block each, so the whole
    survey is a single spawn. Mapping back to the old vocabulary: LoadState=not-found is the dangerous
    "never provisioned" case that `is-enabled` reported as `not-found`; UnitFileState is what
    `is-enabled` printed; ActiveState is what `is-active` printed.
    """
    if not units:
        return {}
    r = subprocess.run(
        ["systemctl", "show", "--property=Id,LoadState,UnitFileState,ActiveState", "--", *units],
        capture_output=True, text=True)
    out: dict[str, tuple[str, str]] = {}
    for block in r.stdout.split("\n\n"):
        if not block.strip():
            continue
        kv = dict(line.split("=", 1) for line in block.strip().splitlines() if "=" in line)
        uid = kv.get("Id")
        if not uid:
            continue
        # A unit systemd has never heard of reports LoadState=not-found; UnitFileState is then empty.
        enabled = "not-found" if kv.get("LoadState") == "not-found" else (kv.get("UnitFileState") or "")
        out[uid] = (enabled, kv.get("ActiveState") or "")
    # `systemctl show` omits nothing normally, but never let a parse miss look like a healthy unit:
    # anything unaccounted for falls back to the per-unit path rather than being silently dropped.
    for u in units:
        if u not in out:
            out[u] = (_sysctl("is-enabled", u), _sysctl("is-active", u))
    return out


def cmd_list(manifest, roles, args) -> int:
    units = resolve(manifest, roles, host_options())
    for name, meta in units.items():
        if args.enable and meta["must"] != "active":       # --enable: only the must-be-running set
            continue
        print(name)
    return 0


def cmd_check(manifest, roles, args) -> int:
    units = resolve(manifest, roles, host_options())
    states = unit_states(sorted(units))            # ONE systemctl call, not two per unit
    gaps, warns, ok = [], [], []
    for name, meta in sorted(units.items()):
        enabled, active = states.get(name, ("not-found", ""))
        installed = enabled != "not-found"
        must = meta["must"]
        if not installed:
            (warns if meta["optional"] else gaps).append(
                (name, "NOT-INSTALLED", "unit file absent — never provisioned"))
        elif must == "active" and active != "active":
            (warns if meta["optional"] else gaps).append(
                (name, "INACTIVE", f"required active, is {active}"))
        else:
            ok.append((name, must, active))
    host = socket.gethostname()
    print(f"supervisor {host}  roles={','.join(roles)}  ok={len(ok)} warn={len(warns)} GAP={len(gaps)}")
    for n, s, why in gaps:
        print(f"  GAP  {n:34} {s:14} {why}")
    for n, s, why in warns:
        print(f"  warn {n:34} {s:14} {why}")
    if args.verbose:
        for n, m, a in ok:
            print(f"  ok   {n:34} {m:8} {a}")
    if gaps and not args.no_alert:
        _publish_alert(host, roles, gaps, args.broker)
    return 1 if gaps else 0


def _publish_alert(host: str, roles: list[str], gaps: list, broker: str) -> None:
    try:
        sys.path.insert(0, str(REPO))
        import paho.mqtt.client as mqtt
        from server.util.mqtt_creds import apply_credentials
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        apply_credentials(c)
        c.connect(broker, 1883, keepalive=10)
        c.loop_start()
        alert = {"id": f"service_missing:{host}", "severity": "critical", "kind": "service_missing",
                 "host": host, "roles": roles,
                 "gaps": [{"unit": n, "state": s, "why": w} for n, s, w in gaps],
                 "message": f"{host}: {len(gaps)} required service(s) missing/inactive: " +
                            ", ".join(n for n, _, _ in gaps)}
        c.publish("home/_alert/new", json.dumps(alert), qos=1)
        # retained per-host supervisor status so cluster-doctor (and the PWA cluster-alert cache) can assert
        # the supervisor itself is fresh. ts must be a real epoch (was None — broke every freshness check).
        import time as _t
        c.publish(f"home/_supervisor/{host}/status",
                  json.dumps({"host": host, "gaps": len(gaps), "units": [n for n, _, _ in gaps],
                              "ts": int(_t.time())}), qos=1, retain=True)
        c.loop_stop(); c.disconnect()
    except Exception as exc:                                   # never let alerting break the check
        print(f"  (alert publish failed: {exc})", file=sys.stderr)


def main() -> int:
    p = argparse.ArgumentParser(description="Required-services resolver + supervisor (manifest-driven)")
    p.add_argument("--roles", nargs="+", help="override this host's roles (else instance/host-roles.yaml)")
    p.add_argument("--manifest", default=str(MANIFEST), type=Path)
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("list", help="list required units for this host")
    pl.add_argument("--enable", action="store_true", help="only the must-be-active units (for install.sh)")
    pl.set_defaults(fn=cmd_list)
    pc = sub.add_parser("check", help="supervisor: flag missing/inactive required units (exit 1 on gap)")
    pc.add_argument("--verbose", action="store_true")
    pc.add_argument("--no-alert", action="store_true")
    pc.add_argument("--broker", default="127.0.0.1")
    pc.set_defaults(fn=cmd_check)
    args = p.parse_args()
    manifest = _load(args.manifest)
    roles = host_roles(args.roles)
    return args.fn(manifest, roles, args)


if __name__ == "__main__":
    raise SystemExit(main())
