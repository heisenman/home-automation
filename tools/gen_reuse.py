#!/usr/bin/env python3
"""Generate the shared-firmware-module table in docs/REUSE.md from each component's header breadcrumb.

ADR-0025 (reuse-first navigation): the capability catalog must be GENERATED from the co-located
module-header breadcrumbs, never hand-maintained — a hand-kept central index rots, and a stale map
lies confidently. Mirrors tools/gen_module_matrix.py.

A shared module declares its breadcrumb in its public header (firmware/components/<m>/include/<m>.h):

  // BREADCRUMB: firmware/components > <module> - <purpose>. Contract: <adr>. Parent: firmware/AGENTS.md.
  // REUSE-WHEN: <one line: when a future agent should reach for this instead of building>

The generated table lives between the GENERATED markers in docs/REUSE.md; the surrounding prose
(other reusable surfaces, vendored list) is hand-written and untouched.

Usage:
  python3 tools/gen_reuse.py            # print the table to stdout (dry run)
  python3 tools/gen_reuse.py --check    # exit 1 if docs/REUSE.md is stale, or a module lacks a breadcrumb
  python3 tools/gen_reuse.py --write    # rewrite the generated region in place
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "firmware" / "components"
SERVER = ROOT / "server"                       # ADR-0025 Pass-3: server packages get a generated table too
REUSE = ROOT / "docs" / "REUSE.md"

GEN_BEGIN = "<!-- GENERATED:reuse (tools/gen_reuse.py --write) — do not edit by hand -->"
GEN_END = "<!-- /GENERATED:reuse -->"
GEN_BEGIN_SRV = "<!-- GENERATED:reuse-server (tools/gen_reuse.py --write) — do not edit by hand -->"
GEN_END_SRV = "<!-- /GENERATED:reuse-server -->"

# Third-party components vendored in-tree — upstream code, not our reusable modules, so exempt from the
# breadcrumb requirement. They are listed as "vendored" in the hand-written prose of REUSE.md.
VENDORED = {"sensirion_gas_index", "sgp30", "sgp40", "sqlite3"}

BREADCRUMB_RE = re.compile(
    r"BREADCRUMB:\s*firmware/components\s*>\s*(?P<mod>\S+)\s*-\s*(?P<purpose>.*?)\.\s*"
    r"Contract:\s*(?P<contract>.*?)\.\s*Parent:")
SERVER_BREADCRUMB_RE = re.compile(   # same shape, declared in each server package's __init__.py
    r"BREADCRUMB:\s*server\s*>\s*(?P<mod>\S+)\s*-\s*(?P<purpose>.*?)\.\s*"
    r"Contract:\s*(?P<contract>.*?)\.\s*Parent:")
REUSEWHEN_RE = re.compile(r"REUSE-WHEN:\s*(?P<when>.+)")  # .+ stops at newline (no MULTILINE needed)


def header_of(comp: Path) -> Path | None:
    inc = comp / "include"
    if (inc / f"{comp.name}.h").exists():
        return inc / f"{comp.name}.h"
    hs = sorted(inc.glob("*.h")) if inc.is_dir() else []
    return hs[0] if hs else None


def scan():
    """Return (rows, missing): rows = [(module, when, contract, header_relpath)]; missing = modules w/o breadcrumb."""
    rows, missing = [], []
    for comp in sorted(p for p in COMPONENTS.iterdir() if p.is_dir()):
        if comp.name in VENDORED:
            continue
        h = header_of(comp)
        txt = h.read_text() if h else ""
        bc = BREADCRUMB_RE.search(txt)
        rw = REUSEWHEN_RE.search(txt)
        if not bc or not rw:
            missing.append(comp.name)
            continue
        rows.append((bc.group("mod"), rw.group("when").strip(),
                     bc.group("contract").strip(), str(h.relative_to(ROOT))))
    return rows, missing


def scan_server():
    """Return (rows, missing) for server packages, parsed from each package's __init__.py breadcrumb.
    Mirrors scan() — a package with no breadcrumb is invisible to the catalog and flagged."""
    rows, missing = [], []
    for pkg in sorted(p for p in SERVER.iterdir() if p.is_dir() and (p / "__init__.py").exists()):
        init = pkg / "__init__.py"
        txt = init.read_text()
        bc = SERVER_BREADCRUMB_RE.search(txt)
        rw = REUSEWHEN_RE.search(txt)
        if not bc or not rw:
            missing.append(pkg.name)
            continue
        rows.append((bc.group("mod"), rw.group("when").strip(),
                     bc.group("contract").strip(), str(init.relative_to(ROOT))))
    return rows, missing


def table(rows) -> str:
    out = ["| Module | Reuse when | Contract | Header |", "|---|---|---|---|"]
    for mod, when, contract, path in rows:
        out.append(f"| `{mod}` | {when} | {contract} | [{path}](../{path}) |")
    return "\n".join(out)


def _render_region(existing: str, begin: str, end: str, rows) -> str:
    block = f"{begin}\n{table(rows)}\n{end}"
    if begin in existing and end in existing:
        return re.sub(re.escape(begin) + r".*?" + re.escape(end), block, existing, flags=re.S)
    return existing.rstrip() + "\n\n" + block + "\n"


def render(existing: str, rows) -> str:
    return _render_region(existing, GEN_BEGIN, GEN_END, rows)


def render_server(existing: str, rows) -> str:
    return _render_region(existing, GEN_BEGIN_SRV, GEN_END_SRV, rows)


def _rendered(existing: str, rows, srows) -> str:
    return render_server(render(existing, rows), srows)


def main():
    args = set(sys.argv[1:])
    rows, missing = scan()
    srows, smissing = scan_server()
    allmissing = [f"fw:{m}" for m in missing] + [f"server:{m}" for m in smissing]
    if allmissing:
        print(f"# WARNING: {len(allmissing)} module(s) with no parseable breadcrumb: {', '.join(allmissing)}",
              file=sys.stderr)
    if "--check" in args:
        cur = REUSE.read_text() if REUSE.exists() else ""
        stale = _rendered(cur, rows, srows) != cur
        if allmissing or stale:
            print("REUSE.md drift or missing breadcrumb — run tools/gen_reuse.py --write", file=sys.stderr)
            sys.exit(1)
        print("REUSE.md up to date")
        return
    if "--write" in args:
        REUSE.write_text(_rendered(REUSE.read_text(), rows, srows))
        print(f"wrote {len(rows)} firmware + {len(srows)} server rows -> {REUSE.relative_to(ROOT)}")
        return
    print(table(rows) + "\n\n" + table(srows))


if __name__ == "__main__":
    main()
