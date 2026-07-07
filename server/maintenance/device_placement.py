"""Placement write path for the spatial room-zoom (map arc 3, docs/design/map-room-fill.md).

Upserts one device's in-room placement in ``instance/device-placement.yaml`` — a line-edit that preserves
the file's per-device comments + column alignment (the file is registry-generated with a
``# area · role · type`` note per device). Atomic (tmp + os.replace) so a concurrent ``/rooms`` read never
sees a torn file. The read path (``build_rooms`` via ``_load_yaml_section``) re-reads the file every
request, so a write is picked up live — no restart, mirroring the relocate/meta endpoints.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def _fmt(v) -> str:
    return "null" if v is None else f"{float(v):.4g}"


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)                       # atomic on POSIX — a reader sees old-or-new, never torn


def write_placement(path, device_id: str, x, y, anchor: str) -> str:
    """Set ``device_id -> {x, y, anchor}`` in the ``placements`` map. Returns ``"updated"`` if the device
    already had a line (comment + alignment preserved) or ``"appended"`` for a new one. ``x,y`` are floats
    in [0,1] or None; ``anchor`` in {n,s,e,w,auto}. The caller validates (see handle_device_placement)."""
    path = Path(path)
    braces = f"{{ x: {_fmt(x)}, y: {_fmt(y)}, anchor: {anchor} }}"
    text = path.read_text() if path.exists() else "placements:\n"
    lines = text.splitlines()
    pat = re.compile(r"^(\s*" + re.escape(device_id) + r":\s*)\{[^}]*\}(.*)$")
    for i, line in enumerate(lines):
        m = pat.match(line)
        if m:
            lines[i] = f"{m.group(1)}{braces}{m.group(2)}"      # keep indent/alignment (g1) + comment (g2)
            _atomic_write(path, "\n".join(lines) + "\n")
            return "updated"
    lines.append(f"  {device_id}: {braces}")                     # not present -> append under placements:
    _atomic_write(path, "\n".join(lines) + "\n")
    return "appended"
