#!/usr/bin/env python3
"""Production line for PG03V3 plugs (ADR-0042): one stock-Tasmota plug on the air-gap WiFi -> enrolled,
branded and running our firmware, in one command.

  python3 tools/plug_line.py 192.168.1.140 plug_02

  1. read the unit's MAC from Tasmota (never printed) and add it to edge/esp32c3-plug/nodes.yaml (gitignored)
  2. enroll the node (mint + LUT + sync to ha-2; --reuse if it is already enrolled) -> secrets.h, version.txt
  3. build the branded image (identity-gated <node>@<fw>), keep a copy in build/images/<node>.bin
  4. tools/plug_takeover.py: check layout -> safeboot -> upload -> wait for the node's hello

Builds share one tree, so run plugs one after another, not in parallel. The plug's WiFi join (Tasmota AP ->
autohome_airgap) is the user's step before this.
"""
import json
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROJ = REPO / "edge/esp32c3-plug"
MANIFEST = PROJ / "nodes.yaml"
BASE_SECRETS = REPO / "edge/esp32c6-dehum/main/secrets.h"   # air-gap wifi/broker/ntp (gitignored)
IDF_EXPORT = Path.home() / "esp/esp-idf/export.sh"


def run(cmd: str, quiet: bool = False) -> str:
    p = subprocess.run(["bash", "-c", cmd], cwd=REPO, capture_output=True, text=True)
    if p.returncode:
        tail = (p.stdout + p.stderr)[-1500:]
        sys.exit(f"FAILED: {cmd.split(' &&')[-1][:80]}\n{tail}")
    return p.stdout


def mac_of(ip: str) -> str:
    with urllib.request.urlopen(f"http://{ip}/cm?cmnd=Status%205", timeout=8) as r:
        mac = json.load(r)["StatusNET"]["Mac"].upper()
    if not re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", mac):
        sys.exit("could not read a MAC from Tasmota Status 5")
    return mac


def add_to_manifest(node: str, mac: str) -> None:
    import yaml
    doc = yaml.safe_load(MANIFEST.read_text()) if MANIFEST.exists() else None
    nodes = (doc or {}).get("nodes") or {}
    have = nodes.get(node)
    if have and have.get("mac", "").upper() != mac:
        sys.exit(f"{node} is already in the manifest with a DIFFERENT MAC — pick another node id")
    for other, ent in nodes.items():
        if other != node and (ent or {}).get("mac", "").upper() == mac:
            sys.exit(f"this unit is already in the manifest as {other}")
    if have:
        return
    with MANIFEST.open("a") as f:
        f.write(f"  {node}:\n    mac: \"{mac}\"\n    target: esp32c3\n    sensor: \"none\"\n    area: staging\n"
                f"    broker: \"mqtt://192.168.1.200:1883\"\n    ota_host: \"192.168.1.210\"\n")


def main() -> None:
    if len(sys.argv) != 3 or not re.fullmatch(r"plug_\d\d", sys.argv[2]):
        sys.exit(__doc__)
    ip, node = sys.argv[1], sys.argv[2]
    print(f"[{node}] reading MAC from {ip}")
    add_to_manifest(node, mac_of(ip))

    print(f"[{node}] enrolling")
    py = REPO / "venv/bin/python"
    enroll = (f"{py} tools/enroll_node.py --node-id {node} --from-manifest --manifest {MANIFEST} "
              f"--base-secrets {BASE_SECRETS} --out {PROJ}/main/secrets.h")
    p = subprocess.run(["bash", "-c", enroll], cwd=REPO, capture_output=True, text=True)
    if p.returncode and "already" in (p.stdout + p.stderr).lower():
        run(enroll + " --reuse")                                  # re-run of a plug enrolled earlier
    elif p.returncode:
        sys.exit("enroll failed:\n" + re.sub(r"[0-9a-f]{32,}", "<redacted>", (p.stdout + p.stderr)[-800:]))
    ver = (PROJ / "version.txt").read_text().strip()
    if not ver.startswith(f"{node}@"):
        sys.exit(f"version.txt is {ver!r}, expected {node}@…")

    print(f"[{node}] building {ver}")
    run(f". {IDF_EXPORT} >/dev/null 2>&1 && cd {PROJ} && idf.py reconfigure >/dev/null && idf.py build >/dev/null")
    img = PROJ / "build/ha-edge-c3-plug.bin"
    if f"{node}@".encode() not in img.read_bytes():
        sys.exit("built image does not carry this node's identity — not uploading")
    keep = PROJ / "build/images" / f"{node}.bin"
    keep.parent.mkdir(exist_ok=True)
    shutil.copy2(img, keep)

    print(f"[{node}] taking over {ip}")
    r = subprocess.run([str(py), "tools/plug_takeover.py", ip, str(keep), "--node", node], cwd=REPO)
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
