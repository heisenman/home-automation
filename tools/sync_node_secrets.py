#!/usr/bin/env python3
"""Keep the edge-node secret LUT identical on .210 (where nodes are enrolled) and ha-2 (the dictator that signs
commands and runs intake). Before this, `enroll_node` wrote .210 only and ha-2's copy silently aged: adopting a
build-time-enrolled node then attempted a TOFU claim the node can never answer (2026-10-04: hvac_c6, dehum_c6,
shades_s3 missing on ha-2; sgp41_mech missing on .210).

    tools/sync_node_secrets.py            # merge both ways (union), back up each file it rewrites
    tools/sync_node_secrets.py --check    # report the difference by NODE NAME only; change nothing (exit 1 if any)

A node present on both sides with DIFFERENT secrets is a conflict: it is never resolved silently (one side
would stop being able to command that node). Resolve by hand, or pass --prefer local|remote.

Each side decrypts/encrypts with its OWN master (instance/.master_pass); plaintext entries only cross inside the
SSH channel (stdin/stdout), never argv, never printed. Remote: $HA2_SSH (default the cluster key to ha-2).
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from server.control import secret_store as S  # noqa: E402

LOCAL_LUT = REPO / "instance" / "node_secrets.enc"
REMOTE = os.environ.get("HA2_SSH", f"ssh -i {Path.home()}/.ssh/id_cluster -o ConnectTimeout=8 visko@192.168.1.210")
REMOTE_REPO = "~/home_automation"
_READ = ("import json; from server.control import secret_store as s; "
         "print(json.dumps(s.load_lut('instance/node_secrets.enc', s.load_master())))")
_MERGE = ("import json, sys, shutil, time, os; from server.control import secret_store as s; "
          "p='instance/node_secrets.enc'; add=json.load(sys.stdin); m=s.load_master(); lut=s.load_lut(p, m); "
          "os.path.exists(p) and shutil.copy2(p, p + '.bak-' + time.strftime('%Y%m%d%H%M%S')); "
          "lut.update(add); s.save_lut(p, m, lut); print(len(lut))")


def remote(py: str, stdin: str | None = None) -> str:
    cmd = shlex.split(REMOTE) + [f"cd {REMOTE_REPO} && venv/bin/python -c {shlex.quote(py)}"]
    r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"ha-2 unreachable or LUT unreadable there: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def plan(local: dict, other: dict, prefer: str | None):
    to_remote = {k: v for k, v in local.items() if k not in other}
    to_local = {k: v for k, v in other.items() if k not in local}
    conflicts = sorted(k for k in local.keys() & other.keys()
                       if (local[k] or {}).get("cmd_secret") != (other[k] or {}).get("cmd_secret"))
    if prefer == "local":
        to_remote.update({k: local[k] for k in conflicts}); conflicts = []
    elif prefer == "remote":
        to_local.update({k: other[k] for k in conflicts}); conflicts = []
    return to_remote, to_local, conflicts


def sync(check: bool = False, prefer: str | None = None, lut_path: Path = LOCAL_LUT, quiet: bool = False) -> int:
    master = S.load_master()
    local = S.load_lut(lut_path, master)
    other = json.loads(remote(_READ) or "{}")
    to_remote, to_local, conflicts = plan(local, other, prefer)
    say = (lambda *a: None) if quiet else print
    say(f"node secrets: .210 has {len(local)}, ha-2 has {len(other)}")
    if to_remote:
        say(f"  ha-2 is missing: {sorted(to_remote)}")
    if to_local:
        say(f"  .210 is missing: {sorted(to_local)}")
    if conflicts:
        print(f"  ⚠ CONFLICT (different secrets for the same node — NOT touched): {conflicts}. "
              f"Re-run with --prefer local|remote once you know which one the node holds.")
    if not (to_remote or to_local):
        say("  in sync" if not conflicts else "  nothing else to do")
        return 2 if conflicts else 0
    if check:
        return 1
    if to_remote:
        n = remote(_MERGE, stdin=json.dumps(to_remote))
        say(f"  ha-2 updated -> {n} node(s) (backup kept beside it)")
    if to_local:
        if lut_path.exists():
            bak = lut_path.with_name(lut_path.name + ".bak-" + time.strftime("%Y%m%d%H%M%S"))
            bak.write_bytes(lut_path.read_bytes()); os.chmod(bak, 0o600)
        local.update(to_local)
        S.save_lut(lut_path, master, local)
        say(f"  .210 updated -> {len(local)} node(s) (backup kept beside it)")
    return 2 if conflicts else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="report differences by name only; write nothing")
    ap.add_argument("--prefer", choices=("local", "remote"), help="resolve conflicts toward this side")
    ap.add_argument("--lut", type=Path, default=LOCAL_LUT)
    a = ap.parse_args()
    try:
        return sync(check=a.check, prefer=a.prefer, lut_path=a.lut)
    except RuntimeError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
