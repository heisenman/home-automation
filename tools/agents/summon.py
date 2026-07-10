#!/usr/bin/env python3
"""summon — coordinate with a peer, preferring the VISIBLE path (Hugh 2026-07-10).

Unifies the two delivery mechanisms so callers can't pick wrong:
  1. If the target has a LIVE interactive session (registered pid alive + a /dev/pts tty), INJECT the
     message into its terminal via tty-inject (TIOCSTI, auto-submit) -> the peer's chat gets a new turn,
     VISIBLE to the human. This is the DEFAULT (Hugh prefers seeing coordination happen).
  2. Otherwise (no live interactive session), or with --headless, fall back to `coord.py wake` -> a headless
     one-shot runner (background autonomy).

  summon.py <target> "<message>" [--task ID] [--headless] [--dry-run]

Requires the host coord-local roster + ~/.claude/host/bin/tty-inject.py (root via sudo; NOPASSWD-listed).
"""
from __future__ import annotations
import argparse, os, re, subprocess, sys

HOSTBIN = os.path.expanduser("~/.claude/host/bin")
CORD_LOCAL = os.path.join(HOSTBIN, "coord-local.py")
TTY_INJECT = os.path.join(HOSTBIN, "tty-inject.py")
COORD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coord.py")

# roster line:  "  dev    pid=2766413 ALIVE           tty=/dev/pts/0  up=4h"
ROSTER_RE = re.compile(r"^\s*(\S+)\s+pid=(\d+)\s+(\w+).*?tty=(\S+)")


def live_session(target: str):
    """Return the target's controlling tty if it's a LIVE interactive session, else None."""
    try:
        out = subprocess.check_output([CORD_LOCAL, "roster"], text=True, timeout=10)
    except Exception as e:
        print(f"summon: roster unavailable: {e}", file=sys.stderr)
        return None
    for line in out.splitlines():
        m = ROSTER_RE.match(line)
        if not m:
            continue
        agent, pid, state, tty = m.group(1), m.group(2), m.group(3), m.group(4)
        if agent == target and state.upper() == "ALIVE" and tty.startswith("/dev/pts/"):
            return tty
    return None


def inject(tty: str, msg: str, dry: bool) -> bool:
    cmd = ["sudo", "-n", "/usr/bin/python3", TTY_INJECT, tty, msg, "--submit"]
    if dry:
        print(f"[dry-run] would INJECT into {tty}: {msg[:80]}…")
        return True
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        print(f"summon: tty-inject failed rc={r.returncode}: {r.stderr.strip()}", file=sys.stderr)
    return r.returncode == 0


def wake(target: str, msg: str, task: str, dry: bool) -> bool:
    cmd = [sys.executable, COORD, "wake", target, "--reason", msg]
    if task:
        cmd += ["--task", task]
    if dry:
        print(f"[dry-run] would WAKE {target} (headless): {msg[:80]}…")
        return True
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    print(r.stdout.strip() or r.stderr.strip())
    return r.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    ap.add_argument("message")
    ap.add_argument("--task", default="")
    ap.add_argument("--headless", action="store_true", help="skip the visible path; go straight to a headless runner")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    tty = None if a.headless else live_session(a.target)
    if tty:
        ok = inject(tty, a.message, a.dry_run)
        print(f"summon: {'INJECTED (visible)' if ok else 'INJECT-FAILED -> falling back to wake'} -> {a.target} @ {tty}")
        if ok:
            return 0
        # inject failed -> fall through to headless so the message isn't lost
    ok = wake(a.target, a.message, a.task, a.dry_run)
    print(f"summon: {'WOKE headless runner' if ok else 'FAILED'} -> {a.target}"
          + ("" if tty else " (no live interactive session)"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
