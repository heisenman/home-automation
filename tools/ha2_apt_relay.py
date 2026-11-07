#!/usr/bin/env python3
"""ha2_apt_relay.py — carry Debian package updates to an offline box (ha-2) from .210 (C1 in
docs/airgap/AIRGAP-RELAY-DESIGN.md; the bundle shape of provisioning/03-sneakernet-updates.md).

ha-2 never reaches the internet, so its apt lists go stale and it can't fetch packages. This tool, run on .210:

  1. reads the target's installed-package list (/var/lib/dpkg/status) and apt sources, and refuses unless the
     target matches .210 (same Debian release, architecture and binary sources);
  2. works out exactly what the target's `apt-get dist-upgrade` would install, using .210's own package lists
     (run `sudo apt update` here first) — the same versions .210 runs;
  3. downloads only those .debs (apt-get download, hash-checked) and lays them out as a minimal local repository
     next to Debian's signed InRelease and Packages index files, exactly as .210 fetched them;
  4. adds tools/apt_bundle_apply.sh and a MANIFEST.sha256, copies the bundle to the target and checks the manifest.

Nothing is installed by this tool. The install is the target's own apt (`sudo apt_bundle_apply.sh`, run by Hugh
on the box), from a file: repository: apt there verifies the InRelease signatures with the TARGET's keyrings, the
Packages files against InRelease and every .deb against Packages. .210 is only a courier — a package altered on
.210 or in transit fails on the target. The MANIFEST only guards the copy.

    tools/ha2_apt_relay.py                  # target ha-2 (192.168.1.200), bundle in ~/apt-bundle-<stamp> there
    tools/ha2_apt_relay.py --dry-run        # just list what the target would get
    tools/ha2_apt_relay.py --target local   # build for THIS box (an end-to-end test; 0 packages if up to date)
    tools/ha2_apt_relay.py --out /media/USB # write the bundle there instead of shipping it (sneakernet)

The InRelease files carry a Valid-Until (Debian security / updates: about a week); the bundle has to be applied
before then, or the target's apt refuses the indexes. The tool prints the deadline.
"""
from __future__ import annotations

import argparse
import email.utils
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LISTS = Path("/var/lib/apt/lists")
APPLY = ROOT / "tools" / "apt_bundle_apply.sh"
HA2 = "192.168.1.200"


def sh(target, cmd: str) -> str:
    """Run a shell command on the target (local, or over ssh) and return stdout."""
    argv = ["bash", "-c", cmd] if target == "local" else ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=30",
                                                          target, cmd]
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout


def binary_sources(text: str) -> list[dict]:
    """Binary (`deb`) entries from sources.list one-line and deb822 text: [{uri, suite, comps, signed_by}]."""
    out = []
    for line in text.splitlines():
        m = re.match(r"^\s*deb\s+(\[[^]]*\]\s+)?(\S+)\s+(\S+)\s+(.+)$", line)
        if m:
            opts = dict(re.findall(r"([\w-]+)=(\S+)", m.group(1) or ""))
            out.append({"uri": m.group(2).rstrip("/"), "suite": m.group(3), "comps": m.group(4).split(),
                        "signed_by": opts.get("signed-by")})
    for stanza in re.split(r"\n\s*\n", text):
        f = dict(re.findall(r"^([\w-]+):\s*(.+)$", stanza, re.M))
        if "deb" in f.get("Types", "").split() and f.get("Enabled", "yes") != "no":
            for uri in f.get("URIs", "").split():
                for suite in f.get("Suites", "").split():
                    out.append({"uri": uri.rstrip("/"), "suite": suite, "comps": f.get("Components", "").split(),
                                "signed_by": f.get("Signed-By")})
    return out


SOURCES_CMD = ("cat /etc/apt/sources.list 2>/dev/null; echo; for f in /etc/apt/sources.list.d/*.list "
               "/etc/apt/sources.list.d/*.sources; do [ -f \"$f\" ] && cat \"$f\" && echo; done")


def lists_name(uri: str) -> str:
    """apt's URItoFileName: scheme dropped, '_' escaped as %5f, '/' -> '_'."""
    rest = uri.split("://", 1)[-1]
    return rest.replace("_", "%5f").replace("/", "_")


def repo_id(uri: str) -> str:
    return uri.split("://", 1)[-1]


def valid_until(inrelease: Path) -> float | None:
    m = re.search(r"^Valid-Until:\s*(.+)$", inrelease.read_text(errors="replace"), re.M)
    return email.utils.parsedate_to_datetime(m.group(1)).timestamp() if m else None


def plan(status_file: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """(installs [(pkg, version)], removals) of a dist-upgrade against `status_file` with .210's lists."""
    r = subprocess.run(["apt-get", "-s", "-o", f"Dir::State::status={status_file}", "-o", "Debug::NoLocking=1",
                        "dist-upgrade"], check=True, capture_output=True, text=True,
                       env={**os.environ, "LC_ALL": "C"})
    inst, rem = [], []
    for line in r.stdout.splitlines():
        m = re.match(r"^Inst (\S+) (?:\[[^]]*\] )?\((\S+) ", line)
        if m:
            inst.append((m.group(1), m.group(2)))
        elif line.startswith("Remv "):
            rem.append(line.split()[1])
    return inst, rem


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--target", default=HA2, help="ssh host, or 'local' (default %(default)s)")
    ap.add_argument("--dry-run", action="store_true", help="list the packages, build nothing")
    ap.add_argument("--out", help="write the bundle into this directory instead of shipping it")
    a = ap.parse_args()
    t = a.target

    # 1. the target must match this box
    here = {"arch": subprocess.run(["dpkg", "--print-architecture"], capture_output=True, text=True).stdout.strip(),
            "os": Path("/etc/debian_version").read_text().split(".")[0].strip()}
    there = {"arch": sh(t, "dpkg --print-architecture").strip(), "os": sh(t, "cut -d. -f1 /etc/debian_version").strip()}
    if here != there:
        sys.exit(f"target {there} doesn't match this box {here}: its packages can't come from here")
    mine = binary_sources(sh("local", SOURCES_CMD))
    theirs = binary_sources(sh(t, SOURCES_CMD))
    key = lambda s: (s["uri"], s["suite"], tuple(sorted(s["comps"])))  # noqa: E731
    if missing := sorted({key(s) for s in theirs} - {key(s) for s in mine}):
        sys.exit(f"the target uses sources this box doesn't: {missing}")

    with tempfile.TemporaryDirectory() as tmp:
        status = Path(tmp) / "status"
        status.write_text(sh(t, "cat /var/lib/dpkg/status"))
        inst, rem = plan(status)
    print(f"{len(inst)} package(s) to install/upgrade on {t}" + (f"; WOULD REMOVE {rem}" if rem else ""))
    for p, v in inst:
        print(f"  {p} {v}")
    if a.dry_run or not inst:
        if not inst:
            print("nothing to carry" + ("" if a.dry_run else "; building an empty bundle so the apply/reboot "
                                         "path can still be run"))
        if a.dry_run:
            return 0

    # 2. the bundle: repo/<uri>/dists/<suite>/{InRelease,<comp>/binary-<arch>/Packages} + repo/<uri>/pool/...
    stamp = time.strftime("%Y%m%d-%H%M")
    work = Path(a.out or tempfile.mkdtemp(prefix="apt-bundle-")) / f"apt-bundle-{stamp}"
    work.mkdir(parents=True)
    try:
        return build_and_ship(a, t, here, theirs, inst, work)
    finally:
        if not a.out and work.parent.exists():
            shutil.rmtree(work.parent, ignore_errors=True)       # the staging copy (shipped or failed)


def build_and_ship(a, t, here, theirs, inst, work) -> int:
    arch = here["arch"]
    src_lines, expiry = [], []
    for s in theirs:
        base = LISTS / lists_name(s["uri"])
        rel = LISTS / f"{lists_name(s['uri'])}_dists_{s['suite']}_InRelease"
        if not rel.exists():
            sys.exit(f"no InRelease for {s['uri']} {s['suite']} here: run `sudo apt update` on this box")
        if (vu := valid_until(rel)) is not None:
            if vu < time.time() + 86400:
                sys.exit(f"{rel.name} expires {time.ctime(vu)}: run `sudo apt update` on this box first")
            expiry.append(vu)
        d = work / "repo" / repo_id(s["uri"]) / "dists" / s["suite"]
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(rel, d / "InRelease")
        comps = []
        for c in s["comps"]:
            pk = Path(f"{base}_dists_{s['suite']}_{c}_binary-{arch}_Packages")
            if pk.exists():                     # a component with no packages for this arch has no list
                (d / c / f"binary-{arch}").mkdir(parents=True, exist_ok=True)
                shutil.copy2(pk, d / c / f"binary-{arch}" / "Packages")
                comps.append(c)
        if comps:
            opt = f" signed-by={s['signed_by']}" if s["signed_by"] else ""
            src_lines.append(f"deb [arch={arch}{opt}] file:@BUNDLE@/repo/{repo_id(s['uri'])} {s['suite']} "
                             + " ".join(comps))
    (work / "sources.list.in").write_text("\n".join(src_lines) + "\n")

    if inst:
        r = subprocess.run(["apt-get", "download", "--print-uris"] + [f"{p}={v}" for p, v in inst],
                           check=True, capture_output=True, text=True)
        # each line: 'URL' local-filename size hash — the local name carries the epoch (%3a), the URL doesn't
        uris = [(ln.split()[0].strip("'"), ln.split()[1]) for ln in r.stdout.splitlines() if ln.startswith("'")]
        dl = work / "dl"
        dl.mkdir()
        subprocess.run(["apt-get", "download", "-q"] + [f"{p}={v}" for p, v in inst], cwd=dl, check=True)
        roots = sorted({s["uri"] for s in theirs}, key=len, reverse=True)
        for u, local in uris:
            root = next(x for x in roots if u.startswith(x + "/"))
            path = urllib.parse.unquote(u[len(root) + 1:])            # pool/main/b/bash/bash_..._amd64.deb
            if not (dl / local).exists():
                sys.exit(f"apt-get download didn't produce {local}")
            dst = work / "repo" / repo_id(root) / path
            dst.parent.mkdir(parents=True, exist_ok=True)
            (dl / local).rename(dst)
        dl.rmdir()
    (work / "PACKAGES.txt").write_text("".join(f"{p} {v}\n" for p, v in inst))
    shutil.copy2(APPLY, work / "apply.sh")
    (work / "apply.sh").chmod(0o755)
    files = sorted(p for p in work.rglob("*") if p.is_file())
    (work / "MANIFEST.sha256").write_text("".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(work)}\n" for p in files))
    size = sum(p.stat().st_size for p in files) / 1e6
    from datetime import datetime
    from zoneinfo import ZoneInfo
    deadline = (datetime.fromtimestamp(min(expiry), ZoneInfo("America/Los_Angeles")).strftime("%a %b %d %H:%M %Z")
                if expiry else "none")
    print(f"bundle {work.name}: {len(inst)} .deb(s), {size:.0f} MB; apply before {deadline}")

    # 3. ship it (or leave it for a USB) and check the manifest on the far side
    if a.out:
        print(f"written to {work}\n  on the target:  sudo <mount>/{work.name}/apply.sh")
        return 0
    if t == "local":
        dest = Path.home() / work.name
        shutil.copytree(work, dest)
        subprocess.run(["sha256sum", "-c", "--quiet", "MANIFEST.sha256"], cwd=dest, check=True)
    else:
        tar = subprocess.Popen(["tar", "cf", "-", "-C", str(work.parent), work.name], stdout=subprocess.PIPE)
        subprocess.run(["ssh", "-o", "BatchMode=yes", t, f"tar xf - -C ~ && cd ~/{work.name} && "
                        "sha256sum -c --quiet MANIFEST.sha256"], stdin=tar.stdout, check=True)
        tar.wait()
        dest = Path("~") / work.name
    print(f"manifest verified on {t}. To install, on that box run:\n\n    sudo {dest}/apply.sh\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
