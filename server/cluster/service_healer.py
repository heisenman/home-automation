#!/usr/bin/env python3
"""
Service healer — the self-healing arm of the required-services supervisor (ADR-000?; plan
crystalline-spinning-spindle). `required_services.py check` is DELIBERATELY read-only (detect + alert);
this is its remediation counterpart, kept as a separate tool so the detector stays non-destructive.

Runs as `ha-service-healer.timer` every ~30s (a oneshot per tick — NOT a long blocking retry loop, so it
survives a crash and never overlaps itself). Each tick:

  1. Detect gaps exactly as `required_services.py cmd_check` does (NOT-INSTALLED vs INACTIVE).
  2. Skip remediation while `instance/.maintenance-fit` is fresh (a deliberate deploy is in progress — don't
     fight it; same flag `failover/healthcheck.sh` honors).
  3. For each gap, escalate through a crash-safe state file (`instance/.healer-state.json`) that accumulates
     ATTEMPTS across ticks — so "restart 5× over ~2.5 min then scream" is wall-clock, not per-invocation:
       - INACTIVE + restartable (ha-*/mosquitto) -> `sudo systemctl restart`; recheck next tick; up to MAX.
       - NOT-INSTALLED (missing unit file, e.g. the 2026-07-26 ha-api-tls wipe) -> request the PEER push the
         canonical unit (peer-repair, request-scoped), install + start locally; cert regen stays local.
       - Exhausted / unrepairable -> SCREAM (critical alert -> ntfy + PWA banner + web-push).
  4. Recovery (a unit that was in the state file is now healthy) clears its state + logs the recovery.

Stage A scope: self-heal + peer-repair + scream. The failover `.unfit` marker (service-aware VIP handoff)
is gated on the `on_fail: failover` manifest tag and lands in Stage C — this tool never touches the VIP.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Reuse the detector rather than duplicate it (plan: import, don't duplicate). tools/ is not a package, so
# load the module by path.
_spec = importlib.util.spec_from_file_location("required_services", REPO / "tools" / "required_services.py")
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)  # exposes: _load, host_roles, resolve, _sysctl, MANIFEST

STATE_FILE = REPO / "instance" / ".healer-state.json"
MAINT_FIT = REPO / "instance" / ".maintenance-fit"
UNFIT_FLAG = REPO / "instance" / ".unfit"      # written for keepalived's healthcheck.sh (Stage C failover)
MAINT_FIT_MAX_AGE = int(os.environ.get("HA_MAINT_FIT_MAX_AGE", "300"))
MAX_ATTEMPTS = int(os.environ.get("HA_HEAL_MAX_ATTEMPTS", "5"))     # 5 × ~30s tick ≈ 2.5 min before scream
# A failover-worthy (on_fail: failover) unit still down this long after self-heal + peer-repair -> write
# .unfit so the standby takes the VIP. > the ~2.5 min scream point, so self-heal gets first crack (5 min total).
UNFIT_THRESHOLD_S = int(os.environ.get("HA_UNFIT_THRESHOLD_S", "300"))
RESTART_TIMEOUT = int(os.environ.get("HA_HEAL_RESTART_TIMEOUT", "20"))
BROKER = os.environ.get("HA_HEAL_BROKER", "127.0.0.1")

# Units we may auto-restart without a password (matches the NOPASSWD sudoers scope: `systemctl restart ha-*`
# + mosquitto). keepalived and anything else escalates instead of being bounced (restarting keepalived could
# itself move the VIP — never do that reflexively).
def _restartable(unit: str) -> bool:
    return unit.startswith("ha-") or unit in ("mosquitto", "mosquitto.service")


def _log(msg: str) -> None:
    print(f"ha.healer — {msg}", flush=True)


def _maintenance_inhibited() -> bool:
    try:
        age = time.time() - MAINT_FIT.stat().st_mtime
        return 0 <= age <= MAINT_FIT_MAX_AGE
    except FileNotFoundError:
        return False


def _detect_gaps(manifest: dict, roles: list[str]) -> dict[str, dict]:
    """Same classification as required_services.cmd_check, returned structured: {unit: {state, why, must}}."""
    units = rs.resolve(manifest, roles, rs.host_options())
    gaps: dict[str, dict] = {}
    # ONE `systemctl show` for the whole set, not two spawns per unit. This runs every 30 SECONDS, so the
    # per-unit form was ~82 processes/run on .210's 41-unit set — around 2.7 forks/s all by itself, the
    # single largest contributor to the idle churn in board task os-idle-churn (it out-spawned even
    # keepalived's two 5-second healthcheck legs). Same classification, same vocabulary.
    states = rs.unit_states([n for n, m in units.items() if not m.get("optional")])
    for name, meta in units.items():
        if meta.get("optional"):
            continue                                          # optional gaps are WARN, never healed/screamed
        enabled, active = states.get(name, ("not-found", ""))
        of = meta.get("on_fail", "heal")
        if enabled == "not-found":
            gaps[name] = {"state": "NOT-INSTALLED", "why": "unit file absent", "must": meta["must"], "on_fail": of}
        elif meta["must"] == "active" and active != "active":
            gaps[name] = {"state": "INACTIVE", "why": f"required active, is {active}", "must": meta["must"],
                          "on_fail": of}
    return gaps


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def _restart(unit: str, dry: bool) -> bool:
    if dry:
        _log(f"[dry-run] would: sudo systemctl restart {unit}")
        return True
    # If we're bouncing ha-api itself, shield the restart from tripping a spurious failover: a fresh
    # .maintenance-fit makes healthcheck.sh report FIT for ≤300s (same mechanism deploys use).
    if unit.startswith("ha-api"):
        try:
            MAINT_FIT.touch()
        except OSError as exc:
            _log(f"warn: could not touch maintenance-fit before {unit} restart: {exc}")
    r = subprocess.run(["sudo", "-n", "systemctl", "restart", unit],
                       capture_output=True, text=True, timeout=RESTART_TIMEOUT)
    if r.returncode == 0:
        return True
    _log(f"restart {unit} FAILED rc={r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
    return False


def _peer_repair_unit(unit: str, dry: bool) -> bool:
    """Request the peer push the canonical unit file, then install+start locally. Request-scoped mutual aid
    (plan Layer 2). Built on failover/cluster-ssh peer-repair; until that lands this returns False (-> scream),
    which is the correct, safe fallback."""
    try:
        from server.cluster import peer_repair                       # noqa: WPS433 (optional dep)
    except Exception:
        return False
    if dry:
        _log(f"[dry-run] would request peer-repair push-unit {unit}")
        return True
    try:
        return bool(peer_repair.recover_unit(unit))
    except Exception as exc:
        _log(f"peer-repair for {unit} failed: {exc}")
        return False


def _mqtt():
    sys.path.insert(0, str(REPO))
    import paho.mqtt.client as mqtt
    from server.util.mqtt_creds import apply_credentials
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    apply_credentials(c)
    c.connect(BROKER, 1883, keepalive=10)
    c.loop_start()
    return c


def _publish_status(host: str, gaps: dict, now: int) -> None:
    """RETAINED per-host supervisor beacon (home/_supervisor/<host>/status), published EVERY run so it tracks
    current truth — a recovery (gaps=0) clears any PWA banner / cluster-alert cache, not just escalations."""
    try:
        c = _mqtt()
        c.publish(f"home/_supervisor/{host}/status",
                  json.dumps({"host": host, "gaps": len(gaps), "units": sorted(gaps),
                              "ts": now}), qos=1, retain=True)
        c.loop_stop(); c.disconnect()
    except Exception as exc:
        _log(f"status publish failed (bus down?): {exc}")


def _scream(host: str, roles: list[str], gaps: dict, unhealed: list[str]) -> None:
    """Edge-triggered critical alert (home/_alert/new) when self-heal + peer-repair could not recover a unit.
    Stable id dedups with the supervisor's service_missing alert; drives ntfy + the PWA banner + web-push."""
    try:
        c = _mqtt()
        alert = {"id": f"service_missing:{host}", "severity": "critical", "kind": "service_missing",
                 "host": host, "roles": roles, "healer": "self-heal+peer-repair exhausted",
                 "gaps": [{"unit": u, "state": gaps[u]["state"], "why": gaps[u]["why"]} for u in unhealed],
                 "message": f"{host}: {len(unhealed)} required service(s) UNRECOVERED after self-heal: "
                            + ", ".join(unhealed)}
        c.publish("home/_alert/new", json.dumps(alert), qos=1)
        c.loop_stop(); c.disconnect()
    except Exception as exc:
        _log(f"scream publish failed (bus down?): {exc}")


def _update_unfit_marker(gaps: dict, state: dict, now: int, dry: bool) -> None:
    """Write/refresh instance/.unfit while a FAILOVER-WORTHY (on_fail: failover) unit has been down past
    UNFIT_THRESHOLD_S despite self-heal + peer-repair (escalated). Remove it otherwise. healthcheck.sh reads
    a FRESH marker as UNFIT -> keepalived hands the VIP to the standby. Refreshed every tick (mtime) so the
    healthcheck freshness window stays satisfied while the gap persists."""
    worthy = [u for u, g in gaps.items()
              if g.get("on_fail") == "failover"
              and state.get(u, {}).get("escalated")
              and (now - state.get(u, {}).get("first_seen", now)) >= UNFIT_THRESHOLD_S]
    if dry:
        if worthy:
            _log(f"[dry-run] would mark UNFIT (failover) for: {worthy}")
        return
    if worthy:
        UNFIT_FLAG.write_text(json.dumps({"units": worthy, "ts": now}))
        _log(f"UNFIT: failover-worthy unit(s) down >{UNFIT_THRESHOLD_S}s -> {worthy}; standby may take the VIP")
    else:
        try:
            UNFIT_FLAG.unlink()
        except FileNotFoundError:
            pass


def heal_once(roles_override: list[str] | None, dry: bool) -> int:
    manifest = rs._load(rs.MANIFEST)
    roles = rs.host_roles(roles_override)
    host = socket.gethostname()
    gaps = _detect_gaps(manifest, roles)
    state = {} if dry else _load_state()          # dry-run never reads/writes persistent state
    now = int(time.time())

    # Recovery: anything tracked but no longer a gap is healthy again.
    for unit in list(state):
        if unit not in gaps:
            _log(f"RECOVERED {unit} (was {state[unit].get('state')}, {state[unit].get('attempts',0)} attempts)")
            del state[unit]

    if not dry:
        _publish_status(host, gaps, now)          # every run tracks current truth (0 gaps clears the banner)

    if not gaps:
        if not dry:
            _save_state(state)
        return 0

    if _maintenance_inhibited():
        _log(f"maintenance-fit fresh — {len(gaps)} gap(s) observed, remediation held: {', '.join(gaps)}")
        if not dry:
            _save_state(state)
        return 0

    unhealed: list[str] = []
    for unit, g in sorted(gaps.items()):
        st = state.setdefault(unit, {"first_seen": now, "attempts": 0, "escalated": False,
                                     "state": g["state"]})
        st["state"] = g["state"]

        if g["state"] == "NOT-INSTALLED":
            if _peer_repair_unit(unit, dry):
                _log(f"peer-repair recovered {unit}; starting")
                _restart(unit, dry)
                continue
            # can't self-heal a missing file without the peer — escalate
            if not st["escalated"]:
                unhealed.append(unit)
        elif _restartable(unit):
            if st["attempts"] < MAX_ATTEMPTS:
                st["attempts"] += 1
                ok = _restart(unit, dry)
                _log(f"heal {unit} ({g['state']}) attempt {st['attempts']}/{MAX_ATTEMPTS} "
                     f"-> {'issued' if ok else 'restart-error'}")
            elif not st["escalated"]:
                unhealed.append(unit)
        else:
            # required but not auto-restartable (e.g. keepalived) — never bounce reflexively, escalate
            if not st["escalated"]:
                _log(f"{unit} ({g['state']}) is not auto-restartable — escalating")
                unhealed.append(unit)

    if unhealed and not dry:
        _scream(host, roles, gaps, unhealed)
    for u in unhealed:
        state[u]["escalated"] = True

    # Stage C: a failover-worthy unit still down past the threshold -> mark UNFIT (keepalived hands off the VIP)
    _update_unfit_marker(gaps, state, now, dry)

    if not dry:
        _save_state(state)
    return 1 if gaps else 0            # non-zero while any gap remains (visible in `systemctl status`)


def main() -> int:
    p = argparse.ArgumentParser(description="Self-healing arm of the required-services supervisor")
    p.add_argument("--roles", nargs="+", help="override host roles (else instance/host-roles.yaml)")
    p.add_argument("--dry-run", action="store_true", help="detect + log actions, change nothing")
    args = p.parse_args()
    return heal_once(args.roles, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
