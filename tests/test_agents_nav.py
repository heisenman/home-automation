"""Drift-guard for the AGENTS.md navigation tree (ADR-0025 Pass 1).

Mirrors test_module_matrix's role for the firmware matrix: the navigation spine must stay
traversable, so a moved/renamed doc that breaks an AGENTS.md link fails HERE rather than
silently rotting into a map that "lies confidently" (ADR-0025's own premise).

Pass-1 scope = assertion (a): every relative markdown link in every AGENTS.md resolves.
Assertions (b) index-coverage of firmware/components + server packages in docs/REUSE.md and
(c) module-header `Parent:` breadcrumbs are Pass 2/3 (they depend on REUSE.md + the breadcrumb
convention, not yet built) — extend this file when those land.
"""
import re
import sys
from pathlib import Path

from tests._harness import run_module

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import gen_reuse as R  # noqa: E402  — drive (b)/(c) off ops's generator so the test can't diverge from it

_PARENT_RE = re.compile(r"Parent:\s*(\S+?\.md)")  # e.g. "Parent: firmware/AGENTS.md."

# Inline markdown links: [text](target). Good enough for the AGENTS.md spine.
_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _agents_files():
    """Every AGENTS.md in the repo (skip the .git dir)."""
    return sorted(p for p in ROOT.rglob("AGENTS.md") if ".git" not in p.parts)


def _relative_link_targets(text):
    """Yield the relative link targets in `text` worth resolving (skip URLs + pure anchors)."""
    for raw in _LINK_RE.findall(text):
        target = raw.strip()
        # external / non-file schemes and pure in-page anchors are not our concern
        if target.startswith(("#", "http://", "https://", "mailto:", "tel:")) or "://" in target:
            continue
        path_part = target.split("#", 1)[0].strip()  # drop any #section anchor
        if path_part:
            yield target, path_part


def test_agents_tree_present():
    """The spine exists: a root map plus subsystem maps, and the root links down to them."""
    files = _agents_files()
    assert (ROOT / "AGENTS.md") in files, "no root AGENTS.md"
    assert len(files) >= 5, f"navigation tree looks too small ({len(files)} AGENTS.md files)"
    root_text = (ROOT / "AGENTS.md").read_text()
    linked_down = {p for _, p in _relative_link_targets(root_text) if p.endswith("AGENTS.md")}
    assert linked_down, "root AGENTS.md links down to no subsystem AGENTS.md — the tree isn't rooted"


def test_all_agents_md_links_resolve():
    """Every relative link in every AGENTS.md points at a file/dir that exists."""
    broken = []
    for f in _agents_files():
        for shown, path_part in _relative_link_targets(f.read_text()):
            if not (f.parent / path_part).resolve().exists():
                broken.append(f"{f.relative_to(ROOT)} -> {shown}")
    assert not broken, "AGENTS.md links point at missing targets:\n  " + "\n  ".join(broken)


def test_checker_detects_a_broken_link():
    """The resolver actually catches breakage (guards against a no-op checker)."""
    good, bad = 0, 0
    for _, path_part in _relative_link_targets("[real](AGENTS.md) [dead](does/not/exist.md)"):
        if (ROOT / path_part).resolve().exists():
            good += 1
        else:
            bad += 1
    assert good == 1 and bad == 1, f"resolver mis-scored the self-test (good={good}, bad={bad})"


# --- ADR-0025 (b) index-coverage + (c) breadcrumb Parent-link (Pass 2/3) ------------------------
# These enforce the capability catalog, driven off tools/gen_reuse.py so they track the generator
# exactly (mirrors how test_module_matrix drives off gen_module_matrix). docs/REUSE.md exists as of
# ops's Pass-1/2 half @19220af.

def test_reuse_md_generated_and_fresh():
    """(b) Every non-vendored firmware component HAS a parseable breadcrumb (else it's invisible to
    the catalog), and docs/REUSE.md's generated table matches those breadcrumbs (no drift)."""
    rows, missing = R.scan()
    assert not missing, (
        f"firmware component(s) with no parseable breadcrumb — invisible to REUSE.md: {missing}. "
        "Add the BREADCRUMB/REUSE-WHEN header (or list as VENDORED in gen_reuse.py).")
    existing = R.REUSE.read_text()
    assert R.render(existing, rows) == existing, "docs/REUSE.md is stale — run tools/gen_reuse.py --write"


def test_reuse_md_links_resolve():
    """The capability catalog can't point at missing files: every relative link in REUSE.md resolves."""
    reuse = ROOT / "docs" / "REUSE.md"
    broken = [f"docs/REUSE.md -> {shown}"
              for shown, path_part in _relative_link_targets(reuse.read_text())
              if not (reuse.parent / path_part).resolve().exists()]
    assert not broken, "REUSE.md links point at missing targets:\n  " + "\n  ".join(broken)


def test_firmware_breadcrumb_parent_links_resolve():
    """(c) From any firmware component you can climb to its authoritative parent: every breadcrumb's
    `Parent:` target resolves. (Missing breadcrumbs are caught by the freshness test above.)"""
    broken = []
    for comp in sorted(p for p in R.COMPONENTS.iterdir() if p.is_dir() and p.name not in R.VENDORED):
        h = R.header_of(comp)
        if not h:
            continue
        m = _PARENT_RE.search(h.read_text())
        if m and not (ROOT / m.group(1)).resolve().exists():
            broken.append(f"{comp.name} -> Parent: {m.group(1)}")
    assert not broken, "breadcrumb Parent links unresolved:\n  " + "\n  ".join(broken)


if __name__ == "__main__":
    run_module(globals())
