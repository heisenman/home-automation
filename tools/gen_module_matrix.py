#!/usr/bin/env python3
"""Generate the device x module matrix (edge/MATRIX.md) from each build's CMakeLists.

ADR-0020 drift-guard: the matrix must reflect what each firmware build ACTUALLY links,
not a hand-maintained guess. A module manifests one of two ways in a build's
`main/CMakeLists.txt idf_component_register(...)`:

  - **fork**   : the module's source file is in SRCS   (edge nodes today)
  - **shared** : the module's shared component is in REQUIRES  (firmware/components/, ADR-0020)

The generated table lives between GENERATED markers in edge/MATRIX.md; the surrounding
prose (platform/transport notes, speculative future columns) is hand-written and untouched.

Usage:
  python3 tools/gen_module_matrix.py            # print the table to stdout (dry run)
  python3 tools/gen_module_matrix.py --check    # exit 1 if edge/MATRIX.md is stale
  python3 tools/gen_module_matrix.py --write     # rewrite the generated region in place

`tests/test_module_matrix.py` runs --check logic so drift fails the suite.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "edge" / "MATRIX.md"

GEN_BEGIN = "<!-- GENERATED:module-matrix (tools/gen_module_matrix.py --write) — do not edit by hand -->"
GEN_END = "<!-- /GENERATED:module-matrix -->"

# Real firmware builds only (speculative E1001/non-Seeed columns stay in the prose below).
# label -> main/CMakeLists.txt, repo-relative.
BUILDS = [
    ("esp32c3", "edge/esp32c3/main/CMakeLists.txt"),
    ("esp32c6", "edge/esp32c6/main/CMakeLists.txt"),
    ("esp32s3-eth", "edge/esp32s3-eth/main/CMakeLists.txt"),
    ("d1001-panel", "provisioning/reterminal/beachhead/main/CMakeLists.txt"),
]

# Catalog rows (ordered). Each module manifests as fork source file(s) and/or a shared
# component name. `sources` matches any listed basename in SRCS; `component` (if set)
# matches an entry in REQUIRES and takes precedence (shared beats fork).
MODULES = [
    ("app_main", {"sources": ["app_main.c", "beachhead_main.c"], "component": None}),
    ("ha_config", {"sources": ["ha_config.c"], "component": None}),
    ("ha_wifi", {"sources": ["ha_wifi.c"], "component": None}),
    ("ha_eth", {"sources": ["ha_eth.c"], "component": None}),
    ("ha_sntp", {"sources": ["ha_sntp.c"], "component": None}),
    ("ha_mqtt", {"sources": ["ha_mqtt.c"], "component": None}),
    ("ble_scan", {"sources": ["ble_scan.c"], "component": "ha_ble_scan"}),
    ("switchbot_decode", {"sources": ["switchbot_decode.c"], "component": "switchbot_decode"}),
    ("gatt_history", {"sources": ["gatt_history.c"], "component": None}),
    ("gatt_exec", {"sources": ["gatt_exec.c"], "component": None}),
    ("ha_ota", {"sources": ["ha_ota.c"], "component": None}),
    ("ha_led", {"sources": ["ha_led.c"], "component": None}),
    ("ha_relay", {"sources": ["ha_relay.c"], "component": None}),
    ("display", {"sources": ["bsp_display.c", "ui_tiles.c"], "component": None}),
]

_SECTION_KEYWORDS = {
    "SRCS", "SRC_DIRS", "INCLUDE_DIRS", "PRIV_INCLUDE_DIRS", "REQUIRES", "PRIV_REQUIRES",
    "REQUIRED_IDF_TARGETS", "EMBED_FILES", "EMBED_TXTFILES", "WHOLE_ARCHIVE", "LDFRAGMENTS",
}


def parse_cmake(path: Path) -> tuple[set[str], set[str]]:
    """Return (srcs basenames, requires) from the file's idf_component_register(...) call."""
    text = path.read_text()
    # strip CMake line comments (no '#' appears inside quoted paths in these files)
    text = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    m = re.search(r"idf_component_register\s*\(", text)
    if not m:
        raise ValueError(f"no idf_component_register in {path}")
    i = m.end()
    depth = 1
    start = i
    while i < len(text) and depth:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
        i += 1
    body = text[start : i - 1]
    tokens = [t.strip('"') for t in body.replace("\n", " ").split()]
    section = None
    srcs: set[str] = set()
    reqs: set[str] = set()
    for tok in tokens:
        if tok in _SECTION_KEYWORDS:
            section = tok
            continue
        if section == "SRCS":
            srcs.add(Path(tok).name)
        elif section in ("REQUIRES", "PRIV_REQUIRES"):
            reqs.add(tok)
    return srcs, reqs


def build_matrix() -> dict[str, dict[str, str]]:
    parsed = {label: parse_cmake(ROOT / rel) for label, rel in BUILDS}
    matrix: dict[str, dict[str, str]] = {}
    for mod, spec in MODULES:
        row: dict[str, str] = {}
        for label, _ in BUILDS:
            srcs, reqs = parsed[label]
            if spec["component"] and spec["component"] in reqs:
                row[label] = "shared"
            elif any(s in srcs for s in spec["sources"]):
                row[label] = "fork"
            else:
                row[label] = "—"
        matrix[mod] = row
    return matrix


def render_table() -> str:
    matrix = build_matrix()
    devices = [label for label, _ in BUILDS]
    header = "| Module | " + " | ".join(devices) + " |"
    sep = "|" + "--------|" + "".join(":-----:|" for _ in devices)
    lines = [header, sep]
    for mod, _ in MODULES:
        cells = " | ".join(matrix[mod][d] for d in devices)
        lines.append(f"| `{mod}` | {cells} |")
    return "\n".join(lines)


def render_region() -> str:
    return f"{GEN_BEGIN}\n\n{render_table()}\n\n{GEN_END}"


def _split(text: str) -> tuple[str, str]:
    """Return (before+marker, marker+after) split points; raise if markers missing/malformed."""
    if GEN_BEGIN not in text or GEN_END not in text:
        raise ValueError(f"{MATRIX} is missing the GENERATED markers")
    before = text[: text.index(GEN_BEGIN)]
    after = text[text.index(GEN_END) + len(GEN_END) :]
    return before, after


def expected_doc() -> str:
    before, after = _split(MATRIX.read_text())
    return before + render_region() + after


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "--print"
    if mode == "--print":
        print(render_table())
        return 0
    if mode == "--check":
        if MATRIX.read_text() == expected_doc():
            print("edge/MATRIX.md is in sync with the builds")
            return 0
        print("DRIFT: edge/MATRIX.md is stale — run: python3 tools/gen_module_matrix.py --write",
              file=sys.stderr)
        return 1
    if mode == "--write":
        MATRIX.write_text(expected_doc())
        print(f"wrote generated matrix into {MATRIX}")
        return 0
    print(f"unknown mode {mode!r}; use --print|--check|--write", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
