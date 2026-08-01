#!/usr/bin/env python3
"""healthcheck_latency_guard.py — scream BEFORE a keepalived track_script crosses its timeout.

Board task `healthcheck-latency-guard`, born from the 2026-07-27 outage.

WHAT WENT WRONG THAT THIS EXISTS TO CATCH
-----------------------------------------
`failover/healthcheck.sh` is keepalived's fitness probe. It used to hit `/api/v1/sensors`, whose cost
scales with dataset size (an unbounded GROUP BY over every authoritative row). As the standby's hot.db
grew to 2.9M rows the probe reached **8.96s against a 4s timeout** — so it could never pass, the leg sat
permanently unfit, and the air-gap VIP **could not move for four days**. Nothing alerted, because a probe
that always fails looks exactly like a probe that is working on a healthy box: keepalived just quietly
holds a lowered priority.

The failure mode is therefore *latency creep into a hard timeout*, and the fix is to watch the margin
rather than the verdict. This alerts at a configurable fraction of the timeout (default 50%) so there is
room to act, plus two related silent-disarm cases the same data exposes for free:

* **stuck-failing** — the probe has returned non-zero for every sample in the window. On a leg with a
  peer that means a failover that will not come back; on a leg without one it is a latent landmine.
* **weight/priority invariant** — a `weight` smaller than the MASTER/BACKUP priority gap means an unfit
  dictator never actually drops below its standby, so health-based failover is disarmed by arithmetic.
  That was a real latent bug (`-40` where `-60` was needed: 150-40=110 > 100). Config-only, but it is the
  same class of silence and free to check while we are parsing the config anyway.

Reads the samples `healthcheck.sh` appends (start, end, exit-code — written with bash builtins so the
5s probe pays no fork), truncates them, and publishes to `home/_alert/new` like the rest of the alert
plumbing (`tools/required_services.py`, the service healer).

Usage:
  healthcheck_latency_guard.py                 # check every leg found in the live keepalived.conf
  healthcheck_latency_guard.py --dry-run       # report only, never publish
  healthcheck_latency_guard.py --warn-frac 0.6 # alert at 60% of timeout instead of 50%
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
KEEPALIVED_CONF = Path("/etc/keepalived/keepalived.conf")
DEFAULT_WARN_FRAC = 0.50
STAT_NAME = "instance/.healthcheck-latency"

# A vrrp_script block: name, then the body up to the closing brace.
_SCRIPT_RE = re.compile(r"vrrp_script\s+(\w+)\s*\{(.*?)\}", re.S)
_INSTANCE_RE = re.compile(r"vrrp_instance\s+(\w+)\s*\{(.*?)\n\}", re.S)


def _field(body: str, key: str):
    """First `key <value>` in a config block, as a string, or None. Comments are stripped first so a
    commented-out value (there are several in this repo's template) is never picked up as live config."""
    clean = "\n".join(line.split("#", 1)[0] for line in body.splitlines())
    m = re.search(rf"^\s*{key}\s+(\S+)", clean, re.M)
    return m.group(1) if m else None


def parse_keepalived(conf: Path) -> list[dict]:
    """Legs found in the live config: script path, timeout/interval, weight, and the priority gap.

    Deliberately parses the INSTALLED config, not `failover/keepalived.conf.tmpl` — the two have drifted
    before (the live household leg kept `weight -40` after the template moved to -60), and it is the live
    one that decides whether your VIP moves.
    """
    if not conf.exists():
        return []
    text = conf.read_text(errors="replace")
    scripts = {}
    for name, body in _SCRIPT_RE.findall(text):
        path = (_field(body, "script") or "").strip('"')
        scripts[name] = {
            "script": path,
            "timeout": float(_field(body, "timeout") or 0) or None,
            "interval": float(_field(body, "interval") or 0) or None,
            "weight": int(_field(body, "weight") or 0),
        }
    legs = []
    for iname, body in _INSTANCE_RE.findall(text):
        tracked = re.search(r"track_script\s*\{(.*?)\}", body, re.S)
        if not tracked:
            continue
        for sname in tracked.group(1).split():
            if sname not in scripts:
                continue
            leg = dict(scripts[sname])
            leg["instance"] = iname
            leg["script_name"] = sname
            leg["state"] = _field(body, "state")
            leg["priority"] = int(_field(body, "priority") or 0)
            legs.append(leg)
    return legs


def stat_path_for(script: str) -> Path:
    """The stat file lives in the checkout that OWNS the script, not this one — .210 runs two legs from two
    separate checkouts (repo + ~/ha-airgap-standby, which is scp-synced, see memory airgap-checkout-drift)."""
    return Path(script).resolve().parents[1] / STAT_NAME


def read_samples(path: Path, *, truncate: bool = True) -> list[tuple[float, float, int]]:
    """(start, duration, exit_code) per recorded run. Truncates so each guard run sees only new samples."""
    if not path.exists():
        return []
    try:
        raw = path.read_text(errors="replace")
        if truncate:
            path.write_text("")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            t0, t1, rc = float(parts[0]), float(parts[1]), int(parts[2])
        except ValueError:
            continue
        if t1 >= t0:
            out.append((t0, t1 - t0, rc))
    return out


def assess(leg: dict, samples: list[tuple[float, float, int]], warn_frac: float) -> list[dict]:
    """Findings for one leg. Empty list = healthy."""
    findings = []
    name = f"{leg['instance']}/{leg['script_name']}"
    timeout = leg.get("timeout")

    if samples and timeout:
        worst = max(s[1] for s in samples)
        mean = sum(s[1] for s in samples) / len(samples)
        frac = worst / timeout
        if frac >= 1.0:
            findings.append({
                "kind": "healthcheck_timeout_exceeded", "severity": "critical", "leg": name,
                "message": (f"{name}: fitness probe took {worst:.2f}s, AT OR OVER its {timeout:.0f}s "
                            f"keepalived timeout — this leg cannot pass its healthcheck, so its VIP "
                            f"cannot move on health. (mean {mean:.2f}s over {len(samples)} samples)")})
        elif frac >= warn_frac:
            findings.append({
                "kind": "healthcheck_latency_high", "severity": "warning", "leg": name,
                "message": (f"{name}: fitness probe reached {worst:.2f}s of its {timeout:.0f}s timeout "
                            f"({frac*100:.0f}%) — act before it crosses; at 100% the VIP silently stops "
                            f"being able to move. (mean {mean:.2f}s over {len(samples)} samples)")})

    if samples and all(s[2] != 0 for s in samples):
        rcs = sorted({s[2] for s in samples})
        findings.append({
            "kind": "healthcheck_stuck_failing", "severity": "critical", "leg": name,
            "message": (f"{name}: fitness probe failed EVERY one of {len(samples)} samples "
                        f"(exit {','.join(map(str, rcs))}) — this box is continuously advertising itself "
                        f"unfit to be dictator.")})

    # Config invariant: an unfit MASTER must actually fall below its standby, or health does nothing.
    if leg.get("state") == "MASTER" and leg.get("weight", 0) < 0 and leg.get("priority"):
        unfit = leg["priority"] + leg["weight"]
        if unfit > 100:      # 100 is this cluster's standard BACKUP priority
            findings.append({
                "kind": "healthcheck_weight_ineffective", "severity": "warning", "leg": name,
                "message": (f"{name}: weight {leg['weight']} only drops priority {leg['priority']}→{unfit}, "
                            f"still above a standard standby's 100 — an unfit dictator would NOT yield the "
                            f"VIP on health alone. Same latent bug fixed on the air-gap leg (-40 → -60).")})
    return findings


def publish(findings: list[dict], broker: str, host: str) -> bool:
    try:
        sys.path.insert(0, str(REPO))
        import paho.mqtt.client as mqtt
        from server.util.mqtt_creds import apply_credentials
        try:                                    # paho v2, falling back to v1 — never hardcode
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except AttributeError:
            c = mqtt.Client()
        apply_credentials(c)
        c.connect(broker, 1883, keepalive=10)
        c.loop_start()
        for f in findings:
            alert = {"id": f"{f['kind']}:{host}:{f['leg']}", "host": host, **f}
            c.publish("home/_alert/new", json.dumps(alert), qos=1)
        c.loop_stop()
        c.disconnect()
        return True
    except Exception as exc:                    # never let alerting break the guard
        print(f"  (alert publish failed: {exc})", file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conf", default=str(KEEPALIVED_CONF))
    ap.add_argument("--warn-frac", type=float, default=DEFAULT_WARN_FRAC,
                    help="alert when the worst sample reaches this fraction of the timeout (default 0.5)")
    ap.add_argument("--broker", default="127.0.0.1")
    ap.add_argument("--dry-run", action="store_true", help="report only; never publish")
    ap.add_argument("--no-truncate", action="store_true", help="leave the stat file intact (debugging)")
    a = ap.parse_args()

    import socket
    host = socket.gethostname()
    legs = parse_keepalived(Path(a.conf))
    if not legs:
        print(f"no keepalived legs found in {a.conf} — nothing to guard")
        return 0

    all_findings = []
    for leg in legs:
        sp = stat_path_for(leg["script"])
        samples = read_samples(sp, truncate=not (a.dry_run or a.no_truncate))
        f = assess(leg, samples, a.warn_frac)
        worst = max((s[1] for s in samples), default=0.0)
        fails = sum(1 for s in samples if s[2] != 0)
        print(f"{leg['instance']}/{leg['script_name']}: {len(samples)} samples, worst {worst:.2f}s / "
              f"timeout {leg.get('timeout')}s, {fails} failing, weight {leg.get('weight')}")
        for x in f:
            print(f"  {x['severity'].upper()}: {x['message']}")
        all_findings += f

    if all_findings and not a.dry_run:
        publish(all_findings, a.broker, host)
    return 1 if all_findings else 0


if __name__ == "__main__":
    sys.exit(main())
