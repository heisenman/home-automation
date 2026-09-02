#!/usr/bin/env python3
"""
Air-gap link watchdog — detect a LIVE-BUT-DEAD WiFi bridge leg and reassociate it (ADR-0039).

THE FAILURE THIS EXISTS FOR (2026-09-02, ~03:01-03:10 UTC). .210's `mt7921e` held an association the AP
had stopped forwarding for. Every WiFi surface read perfectly healthy — associated, signal -33 dBm,
`beacon loss: 0`, and the AP was still hardware-ACKing our frames (`last ack signal: -32 dBm`) — while
ARP failed for EVERY host on the air-gap /24, the gateway included. nginx lost its upstream and served
502 to the household; the only symptom anywhere was a wall-panel error a human had to notice. The router
was NOT at fault: `airgap_router_pm` metered a flat 7.0 W for every sample across the whole window, so
there was no reboot or brownout to blame. `systemctl restart ha-airgap-bridge` (one reassociation) fixed
it instantly.

That is the whole point: the radio being up is NOT evidence the link works, so nothing that reads radio
state can catch this. We probe L3 and act on what the probe says.

  * probe        — gateway (ping) + ha-2 (ping OR TCP). Two hosts, two methods, so ICMP being deprioritized
                   or rate-limited under load can't manufacture a false outage.
  * discriminate — DOWN means NOTHING answered. If the gateway answers but ha-2 doesn't, the LINK is fine
                   and ha-2 is the problem: we alert and DO NOT reassociate. Bouncing our own radio because
                   a peer died is the same shared-fate error `provisioning/required-services.yaml` warns
                   about for `on_fail: failover` — the remedy has to be able to fix the fault.
  * act          — after FAIL_TICKS consecutive all-down ticks, `systemctl restart ha-airgap-bridge`
                   (inside the NOPASSWD `ha-*` sudoers scope), rate-limited by COOLDOWN_S.
  * give up      — after MAX_REASSOC failed reassociations, SCREAM once and stop acting. If reassociating
                   didn't fix it, it isn't the zombie-association fault and hammering the radio only makes
                   a human's diagnosis harder. That branch needs hands.

Alerts go to the LOCAL broker (127.0.0.1), never the VIP: a mechanism whose job is to report that the
air-gap link is down cannot itself depend on the air-gap link ([[failover-primitives-not-on-vip]]).

Oneshot per tick, driven by `ha-airgap-linkwatch.timer` (~30s) — crash-safe, and systemd won't overlap
ticks. Hysteresis lives in `instance/.linkwatch-state.json` so "3 consecutive failures" is wall-clock
across ticks, exactly like `service_healer.py` accumulates restart attempts.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Overridable so a fault-injection drill can run against a scratch state file without disturbing the live
# hysteresis counters (see docs/adr/ADR-0039 "Verification").
STATE_FILE = Path(os.environ.get("HA_LINKWATCH_STATE", str(REPO / "instance" / ".linkwatch-state.json")))
MAINT_FIT = REPO / "instance" / ".maintenance-fit"
MAINT_FIT_MAX_AGE = int(os.environ.get("HA_MAINT_FIT_MAX_AGE", "300"))

# The air-gap leg and what we probe on it. GATEWAY is the L2 next hop — the single most diagnostic target,
# because a dead gateway means the fault is at OUR radio, not at any one peer.
IFACE = os.environ.get("AIRGAP_WIFI_IFACE", "wlp2s0")
GATEWAY = os.environ.get("HA_AIRGAP_GATEWAY", "192.168.1.1")
PEER = os.environ.get("HA_AIRGAP_PEER", "192.168.1.210")          # ha-2 on the air-gap side
PEER_PORT = int(os.environ.get("HA_AIRGAP_PEER_PORT", "8123"))    # ha-2's API — a real service, not just ICMP
BRIDGE_UNIT = os.environ.get("HA_AIRGAP_BRIDGE_UNIT", "ha-airgap-bridge.service")

FAIL_TICKS = int(os.environ.get("HA_LINKWATCH_FAIL_TICKS", "3"))       # ~90s at a 30s tick before we act
MAX_REASSOC = int(os.environ.get("HA_LINKWATCH_MAX_REASSOC", "3"))     # then scream; it isn't our radio
COOLDOWN_S = float(os.environ.get("HA_LINKWATCH_COOLDOWN_S", "120"))   # never bounce the radio faster
STABLE_S = float(os.environ.get("HA_LINKWATCH_STABLE_S", "120"))       # "up" this long = a real recovery
PROBE_TIMEOUT_S = float(os.environ.get("HA_LINKWATCH_PROBE_TIMEOUT_S", "2"))
RESTART_TIMEOUT = int(os.environ.get("HA_LINKWATCH_RESTART_TIMEOUT", "30"))
BROKER = os.environ.get("HA_LINKWATCH_BROKER", "127.0.0.1")
BROKER_PORT = int(os.environ.get("HA_LINKWATCH_BROKER_PORT", "1883"))


def _log(msg: str) -> None:
    print(f"ha.linkwatch — {msg}", flush=True)


# ── input validation ────────────────────────────────────────────────────────────────────────────────────
# Every tunable above is env-settable, and two of them reach a subprocess argv. We use list-form subprocess
# (so there is no shell to inject into), but argv position alone isn't enough: a probe host of "-f" would
# make `ping` FLOOD, and an arbitrary unit name would hand `sudo systemctl restart` a target we never
# intended. Neither is a privilege escalation — this runs as visko, who already holds NOPASSWD on
# `systemctl restart ha-*` — but both are avoidable footguns, so constrain the shapes at startup.
def _valid_host(h: str) -> bool:
    import ipaddress
    import re
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return bool(re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?", h))


def _valid_unit(u: str) -> bool:
    import re
    # only ha-* — matches the sudoers NOPASSWD scope, so we can never ask for something it would deny
    return bool(re.fullmatch(r"ha-[A-Za-z0-9@._-]+(\.service)?", u))


def _check_config() -> None:
    for name, host in (("HA_AIRGAP_GATEWAY", GATEWAY), ("HA_AIRGAP_PEER", PEER)):
        if not _valid_host(host):
            raise SystemExit(f"ha.linkwatch — refusing to start: {name}={host!r} is not a valid host")
    if not _valid_unit(BRIDGE_UNIT):
        raise SystemExit(f"ha.linkwatch — refusing to start: HA_AIRGAP_BRIDGE_UNIT={BRIDGE_UNIT!r} "
                         f"is not an ha-* unit")


# ── pure decision core (no network, no clock, no filesystem — this is what the tests drive) ─────────────
@dataclass
class Probes:
    """One tick's observation of the air-gap net."""
    gateway_ok: bool
    peer_ok: bool

    @property
    def any_ok(self) -> bool:
        return self.gateway_ok or self.peer_ok


@dataclass
class Decision:
    link: str                                   # "up" | "down" | "peer_down"
    action: str = "none"                        # "none" | "reassociate" | "scream"
    alerts: list[dict] = field(default_factory=list)
    note: str = ""


def _alert(kind: str, severity: str, message: str, **extra) -> dict:
    # Stable id per kind so a re-fire dedups in the alert engine / PWA banner rather than stacking.
    return {"id": f"{kind}:{IFACE}", "kind": kind, "severity": severity, "iface": IFACE,
            "message": message, **extra}


def decide(p: Probes, state: dict, now: float, *, inhibited: bool = False) -> Decision:
    """Pure: (observation, prior state, now) -> what to do. MUTATES `state` (the caller persists it).

    Edge-triggered throughout: an alert fires on the TRANSITION, not every tick, so a long outage is one
    notification and not a stream of them.
    """
    if p.any_ok:
        # ── the link carries traffic ────────────────────────────────────────────────────────────────
        d = Decision(link="up")
        was_down = state.get("consecutive_down", 0) >= FAIL_TICKS
        if was_down:
            down_s = now - state.get("link_down_since", now)
            d.alerts.append(_alert("airgap_link_up", "info",
                                   f"air-gap link on {IFACE} RECOVERED after {down_s:.0f}s "
                                   f"({state.get('reassoc_attempts', 0)} reassociation(s))",
                                   down_s=round(down_s)))
            state["link_up_since"] = now
        state.setdefault("link_up_since", now)
        state["consecutive_down"] = 0
        state["link_down_since"] = None
        state["escalated"] = False
        state["link_down_alerted"] = False       # re-arm the outage edge for the NEXT outage
        # Only forgive the reassociation budget once the link has been STABLY up. Without this a flapping
        # link resets the counter every other tick and we'd bounce the radio forever.
        if now - state.get("link_up_since", now) >= STABLE_S:
            state["reassoc_attempts"] = 0

        # The link is fine but ha-2 specifically is unreachable. Reassociating cannot fix that, so we say
        # so and keep our hands off the radio.
        if p.gateway_ok and not p.peer_ok:
            d.link = "peer_down"
            if not state.get("peer_down_alerted"):
                state["peer_down_alerted"] = True
                d.alerts.append(_alert("airgap_peer_down", "critical",
                                       f"air-gap link is UP (gateway {GATEWAY} answers) but peer {PEER} "
                                       f"does NOT — this is ha-2, not the link; NOT reassociating",
                                       peer=PEER))
            d.note = "peer unreachable, link healthy — no radio action"
        elif state.get("peer_down_alerted"):
            state["peer_down_alerted"] = False
            d.alerts.append(_alert("airgap_peer_up", "info", f"air-gap peer {PEER} is reachable again",
                                   peer=PEER))
        return d

    # ── nothing on the air-gap net answered ────────────────────────────────────────────────────────────
    state["consecutive_down"] = state.get("consecutive_down", 0) + 1
    n = state["consecutive_down"]
    if state.get("link_down_since") is None:
        state["link_down_since"] = now
    d = Decision(link="down")

    if n < FAIL_TICKS:
        d.note = f"down {n}/{FAIL_TICKS} — within hysteresis, not acting yet"
        return d

    # Crossing the threshold is the alertable edge. Latch it on a FLAG rather than `n == FAIL_TICKS`: a
    # missed tick (crash, a long reassociate) can step the counter straight past the equality and we would
    # then never announce an outage that is genuinely happening.
    if not state.get("link_down_alerted"):
        state["link_down_alerted"] = True
        d.alerts.append(_alert("airgap_link_down", "critical",
                               f"air-gap link on {IFACE} is DOWN — neither gateway {GATEWAY} nor peer "
                               f"{PEER} answered on {n} consecutive probes (radio may still read "
                               f"'associated'; that is the known false-healthy mode)",
                               consecutive=n))

    if inhibited:                                # a deliberate deploy is in flight — observe, don't fight it
        d.note = "maintenance-fit fresh — remediation held"
        return d

    if state.get("reassoc_attempts", 0) >= MAX_REASSOC:
        # Reassociating demonstrably isn't the fix. Stop touching the radio and escalate to a human once.
        if not state.get("escalated"):
            state["escalated"] = True
            d.action = "scream"
            d.alerts.append(_alert("airgap_link_unrecovered", "critical",
                                   f"air-gap link on {IFACE} STILL DOWN after {MAX_REASSOC} "
                                   f"reassociation(s) — this is NOT the zombie-association fault; the "
                                   f"router or ha-2 needs hands. No further automatic action.",
                                   reassoc_attempts=state.get("reassoc_attempts", 0)))
        else:
            d.note = "escalated — no further automatic action"
        return d

    since_action = now - state.get("last_action_ts", 0.0)
    if since_action < COOLDOWN_S:
        d.note = f"cooldown {COOLDOWN_S - since_action:.0f}s remaining before another reassociation"
        return d

    state["reassoc_attempts"] = state.get("reassoc_attempts", 0) + 1
    state["last_action_ts"] = now
    d.action = "reassociate"
    d.note = f"reassociation {state['reassoc_attempts']}/{MAX_REASSOC}"
    return d


# ── effects (network / systemd / bus) ──────────────────────────────────────────────────────────────────
def ping(host: str, timeout_s: float = PROBE_TIMEOUT_S) -> bool:
    """One ICMP echo. Unprivileged (`ping` carries cap_net_raw); -W is the per-reply wait in seconds."""
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", str(int(max(1, timeout_s))), host],
                           capture_output=True, timeout=timeout_s + 3)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def tcp_ok(host: str, port: int, timeout_s: float = PROBE_TIMEOUT_S) -> bool:
    """A real TCP handshake — survives ICMP being filtered or deprioritized."""
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def observe() -> Probes:
    gw = ping(GATEWAY)
    # Cheapest sufficient probe: if the gateway answered the link carries traffic, so only spend a TCP
    # connect when we still need to tell "link dead" from "peer dead".
    peer = ping(PEER) or tcp_ok(PEER, PEER_PORT)
    return Probes(gateway_ok=gw, peer_ok=peer)


def reassociate(dry: bool) -> bool:
    """Bounce the bridge unit — it re-runs `ip addr`/`wpa_supplicant` and forces a fresh association."""
    if dry:
        _log(f"[dry-run] would: sudo systemctl restart {BRIDGE_UNIT}")
        return True
    try:
        r = subprocess.run(["sudo", "-n", "systemctl", "restart", BRIDGE_UNIT],
                           capture_output=True, text=True, timeout=RESTART_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log(f"reassociate FAILED to run: {exc}")
        return False
    if r.returncode == 0:
        _log(f"reassociated: restarted {BRIDGE_UNIT}")
        return True
    _log(f"reassociate FAILED rc={r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
    return False


def _mqtt():
    sys.path.insert(0, str(REPO))
    import paho.mqtt.client as mqtt
    from server.util.mqtt_creds import apply_credentials
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    apply_credentials(c)
    c.connect(BROKER, BROKER_PORT, keepalive=10)
    c.loop_start()
    return c


def publish(alerts: list[dict], link: str, probes: Probes, state: dict, dry: bool) -> None:
    """Edge alerts -> home/_alert/new; a RETAINED beacon every tick so a dashboard reads current truth
    (and a recovery clears the banner) rather than only ever seeing the escalations."""
    beacon = {"iface": IFACE, "link": link, "gateway_ok": probes.gateway_ok, "peer_ok": probes.peer_ok,
              "consecutive_down": state.get("consecutive_down", 0),
              "reassoc_attempts": state.get("reassoc_attempts", 0), "ts": int(time.time())}
    if dry:
        for a in alerts:
            _log(f"[dry-run] would alert: {a['kind']} — {a['message']}")
        _log(f"[dry-run] would beacon: {beacon}")
        return
    try:
        c = _mqtt()
        for a in alerts:
            a.setdefault("ts", _iso())
            c.publish("home/_alert/new", json.dumps(a), qos=1)
            _log(f"{a['kind'].upper()}: {a['message']}")
        c.publish(f"home/_airgap/{IFACE}/link", json.dumps(beacon), qos=1, retain=True)
        c.loop_stop()
        c.disconnect()
    except Exception as exc:                     # a down bus must never take the watchdog with it
        _log(f"publish failed (bus down?): {exc}")


def _iso() -> str:
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def _maintenance_inhibited() -> bool:
    try:
        age = time.time() - MAINT_FIT.stat().st_mtime
        return 0 <= age <= MAINT_FIT_MAX_AGE
    except FileNotFoundError:
        return False


def tick(dry: bool) -> int:
    probes = observe()
    state = {} if dry else _load_state()
    now = time.time()
    d = decide(probes, state, now, inhibited=_maintenance_inhibited())

    _log(f"gateway({GATEWAY})={'ok' if probes.gateway_ok else 'FAIL'} "
         f"peer({PEER})={'ok' if probes.peer_ok else 'FAIL'} -> link={d.link}"
         + (f" | {d.note}" if d.note else ""))

    if d.action == "reassociate" and not reassociate(dry):
        # The restart itself failed (sudo denied, unit missing) — that's a distinct, human-visible fault.
        d.alerts.append(_alert("airgap_reassociate_failed", "critical",
                               f"air-gap link is down and `systemctl restart {BRIDGE_UNIT}` FAILED — "
                               f"the watchdog cannot self-heal this box"))

    publish(d.alerts, d.link, probes, state, dry)
    if not dry:
        _save_state(state)
    return 0 if d.link == "up" else 1            # non-zero while degraded — visible in `systemctl status`


def main() -> int:
    p = argparse.ArgumentParser(description="Air-gap WiFi link watchdog (probe L3, reassociate on a "
                                            "live-but-dead leg)")
    p.add_argument("--dry-run", action="store_true", help="probe + log the decision, change nothing")
    p.add_argument("--status", action="store_true", help="print the persisted state and exit")
    args = p.parse_args()
    if args.status:
        print(json.dumps(_load_state(), indent=2))
        return 0
    _check_config()          # fail loudly at startup, never probe/act on a malformed target
    return tick(args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
