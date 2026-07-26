#!/usr/bin/env python3
"""
Peer-repair — request-scoped mutual aid between cluster peers (plan crystalline-spinning-spindle, Layer 2;
DIRECTIVE peer-repair-request-scoped). When the healer finds a required unit whose FILE is missing (the
2026-07-26 ha-api-tls wipe: nothing to restart), it can't self-heal locally — but the peer holds the
canonical copy. This pulls that copy over the existing reconcile-agent SSH allowlist and installs it.

Discipline (Hugh): the requester decides and pulls ONE named, manifest-bounded artifact; the requestee only
ever SERVES a repo file it already holds (existing `scp -f $REPO/$REL` allow, `..`-guarded) — it takes no
independent action. The privileged install is the bounded `failover/cluster-ssh/install-unit.sh` helper.

Never transmits secrets: TLS keys/certs are per-box self-signed and regenerated LOCALLY (tools/gen_tls.py).
Box-local fields (HA_VIP) are rewritten to this host's value before install, so the peer's generic/other-net
unit doesn't carry the wrong VIP.
"""
from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from server.cluster.state import read_cluster_env          # noqa: E402


def _log(msg: str) -> None:
    print(f"ha.peer_repair — {msg}", flush=True)


def _cfg() -> dict:
    env = read_cluster_env()
    return {
        "host": env.get("PEER_HOST", ""),
        "port": env.get("CLUSTER_SSH_PORT", "22"),
        "key": env.get("CLUSTER_KEY", str(Path.home() / ".ssh/id_cluster")),
        "remote_repo": env.get("REMOTE_REPO", str(REPO)),
        "vip": env.get("VIP", ""),
    }


def _is_required(name: str) -> bool:
    """Only a unit this host actually requires may be pulled — the manifest is the allowlist."""
    r = subprocess.run([str(REPO / "venv/bin/python3"), str(REPO / "tools/required_services.py"), "list"],
                       capture_output=True, text=True)
    return name in r.stdout.split()


def _scp_from_peer(cfg: dict, remote_rel: str, local: Path) -> bool:
    src = f"visko@{cfg['host']}:{cfg['remote_repo']}/{remote_rel}"
    cmd = ["scp", "-O", "-P", str(cfg["port"]), "-i", cfg["key"],
           "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new",
           src, str(local)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        _log(f"scp {remote_rel} from {cfg['host']} failed: {(r.stderr or r.stdout).strip()[:200]}")
    return r.returncode == 0


def _rewrite_vip(unit_text: str, vip: str) -> str:
    if not vip:
        return unit_text
    try:
        ipaddress.ip_address(vip)
    except ValueError:
        return unit_text
    # only rewrite an existing HA_VIP env line to THIS host's VIP; leave everything else verbatim
    return re.sub(r"(?m)^(Environment=HA_VIP=).*$", rf"\g<1>{vip}", unit_text)


def _ensure_tls_certs(name: str) -> None:
    """ha-api-tls won't start without instance/tls/server.{key,crt}; regenerate locally if absent."""
    if "api-tls" not in name:
        return
    tls = REPO / "instance/tls"
    if (tls / "server.key").exists() and (tls / "server.crt").exists():
        return
    cfg = _cfg()
    sans = ["127.0.0.1", "localhost"]
    if cfg["vip"]:
        sans.insert(0, cfg["vip"])
    _log(f"regenerating local TLS certs (SANs {','.join(sans)})")
    subprocess.run([str(REPO / "venv/bin/python3"), str(REPO / "tools/gen_tls.py"),
                    "--out-dir", str(tls), "--san", ",".join(sans), "--force"],
                   capture_output=True, text=True, timeout=30)


def recover_unit(name: str) -> bool:
    """Pull + install the canonical <name> from the peer. Returns True if installed (caller then starts it)."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.(service|timer)", name):
        _log(f"refusing malformed unit name {name!r}")
        return False
    if not _is_required(name):
        _log(f"{name} is not a required unit here — refusing peer-repair")
        return False
    cfg = _cfg()
    if not cfg["host"]:
        _log("no PEER_HOST in cluster.env — cannot peer-repair")
        return False

    staged = Path("/tmp") / name
    if not _scp_from_peer(cfg, f"systemd/{name}", staged):
        return False
    text = staged.read_text()
    if "[Unit]" not in text:
        _log(f"fetched {name} does not look like a unit — refusing")
        return False
    staged.write_text(_rewrite_vip(text, cfg["vip"]))

    _ensure_tls_certs(name)

    r = subprocess.run(["sudo", "-n", str(REPO / "failover/cluster-ssh/install-unit.sh"), name],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        _log(f"install-unit {name} failed: {(r.stderr or r.stdout).strip()[:200]}")
        return False
    _log(f"peer-repair installed {name} from {cfg['host']}")
    return True


def request_peer_restart(unit: str) -> bool:
    """Ask the PEER to rerun one of ITS OWN ha-* units (the 'push a rerun of services over ssh' case). The
    requestee only honors this via the reconcile-agent allowlist (`sudo -n systemctl restart <ha-*>`), doing
    exactly the named restart and nothing else. Request-scoped: WE decide, the peer obeys the one verb."""
    if not re.fullmatch(r"ha-[A-Za-z0-9._-]+\.(service|timer)", unit):
        _log(f"refusing peer-restart of non-ha unit {unit!r}")
        return False
    cfg = _cfg()
    if not cfg["host"]:
        return False
    cmd = ["ssh", "-p", str(cfg["port"]), "-i", cfg["key"], "-o", "BatchMode=yes",
           "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new",
           f"visko@{cfg['host']}", f"sudo -n systemctl restart {unit}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        _log(f"peer-restart {unit} on {cfg['host']} failed: {(r.stderr or r.stdout).strip()[:200]}")
        return False
    _log(f"peer-restart {unit} requested on {cfg['host']}")
    return True


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Peer-repair: pull+install a missing required unit from the peer")
    p.add_argument("action", choices=["recover", "fetch", "peer-restart"])
    p.add_argument("unit")
    a = p.parse_args()
    if a.action == "fetch":                                  # test helper: fetch-only, no install
        cfg = _cfg()
        ok = _scp_from_peer(cfg, f"systemd/{a.unit}", Path("/tmp") / a.unit)
        print("fetched" if ok else "fetch failed")
        return 0 if ok else 1
    if a.action == "peer-restart":
        return 0 if request_peer_restart(a.unit) else 1
    return 0 if recover_unit(a.unit) else 1


if __name__ == "__main__":
    raise SystemExit(main())
