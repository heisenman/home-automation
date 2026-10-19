#!/usr/bin/env python3
"""Take a stock-Tasmota PG03V3 plug over to our firmware, over WiFi (ADR-0042). Stdlib only.

  python3 tools/plug_takeover.py 192.168.1.117 edge/esp32c3-plug/build/ha-edge-c3-plug.bin --node plug_01

Steps (the Tasmota web page's own sequence, reproduced):
  1. check it IS Tasmota on an ESP32-C3 with the safeboot layout we planned for (partition table read live
     through Tasmota's Berry console; refuses anything else — nothing is written until this passes)
  2. GET /u4?u4=fct&api=  -> restart into SAFEBOOT; poll until it answers "true"
     (the main firmware can only write the 832 K safeboot slot — a ~1 MB image is refused there)
  3. POST /u2?fsz=<bytes> (form field u2) -> safeboot writes app0 @0xE0000 and boots it
  4. our image migrates the partition table (ha_partmig) and comes up; with --node, wait for
     home/edge/<node>/hello on the broker and print it

The plug must already be on the air-gap WiFi (Tasmota setup AP -> autohome_airgap: a user step).
"""
import argparse
import json
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

PLANNED = {"nvs": 0x9000, "otadata": 0xE000, "safeboot": 0x10000, "app0": 0xE0000}   # offsets ota_0 relies on


def get(ip: str, path: str, timeout: float = 8) -> str:
    with urllib.request.urlopen(f"http://{ip}{path}", timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def tasmota_cmd(ip: str, cmd: str) -> dict:
    return json.loads(get(ip, "/cm?" + urllib.parse.urlencode({"cmnd": cmd})))


def check(ip: str) -> None:
    fw = tasmota_cmd(ip, "Status 2")["StatusFWR"]
    if "ESP32-C3" not in fw.get("Hardware", ""):
        sys.exit(f"refusing: not an ESP32-C3 ({fw.get('Hardware')})")
    raw = bytes.fromhex(tasmota_cmd(ip, "Br import flash; return flash.read(0x8000, 0x100).tohex()")["Br"])
    parts = {}
    for i in range(0, len(raw), 32):
        e = raw[i:i + 32]
        magic = struct.unpack("<H", e[:2])[0]
        if magic == 0xEBEB:
            continue
        if magic != 0x50AA:
            break
        parts[e[12:28].rstrip(b"\0").decode()] = struct.unpack("<I", e[4:8])[0]
    bad = {k: hex(v) for k, v in PLANNED.items() if parts.get(k) != v}
    if bad:
        sys.exit(f"refusing: partition table differs from the planned Tasmota layout ({bad}; live {parts})")
    print(f"ok: Tasmota {fw['Version']} on {fw['Hardware']}, safeboot layout as planned")


def to_safeboot(ip: str) -> None:
    try:
        r = get(ip, "/u4?u4=fct&api=")
    except Exception:
        r = ""
    if r.strip() == "true":
        print("already in safeboot")
        return
    print("restarting into safeboot ...")
    for _ in range(40):
        time.sleep(3)
        try:
            if get(ip, "/u4?u4=fct&api=", timeout=4).strip() == "true":
                print("in safeboot")
                return
        except Exception:
            pass
    sys.exit("safeboot did not answer within 2 min — check the plug, nothing was written")


def upload(ip: str, image: Path) -> None:
    data = image.read_bytes()
    if data[:1] != b"\xE9":
        sys.exit("refusing: not an ESP app image (no 0xE9 magic)")
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"u2\"; filename=\"firmware.bin\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(f"http://{ip}/u2?fsz={len(data)}", data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=240) as r:
        page = r.read().decode("utf-8", "replace")
    if "Successful" not in page:
        sys.exit("upload NOT successful — Tasmota said: " + " ".join(
            t for t in page.replace(">", "<").split("<") if t.strip() and "{" not in t and "(" not in t)[-200:])
    print(f"uploaded {len(data)} B — safeboot is booting it")


def wait_hello(node: str, broker: str) -> None:
    print(f"waiting for home/edge/{node}/hello ...")
    try:
        out = subprocess.run(["mosquitto_sub", "-h", broker, "-t", f"home/edge/{node}/hello", "-C", "1",
                              "-W", "120"], capture_output=True, text=True, timeout=130).stdout.strip()
    except Exception as e:
        sys.exit(f"no hello ({e}) — the table write may still be fine; check the router leases")
    if not out:
        sys.exit("no hello within 2 min — check the router leases / power-cycle once")
    h = json.loads(out)
    h.pop("mac", None)                                   # MACs stay out of logs (AGENTS.md)
    print("UP:", json.dumps(h))
    if h.get("slot") != "ota_0":
        print("⚠ slot is not ota_0 — the partition table may not have migrated")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ip")
    ap.add_argument("image", type=Path)
    ap.add_argument("--node", help="node id baked into the image: wait for its hello")
    ap.add_argument("--broker", default="192.168.1.200")
    a = ap.parse_args()
    check(a.ip)
    to_safeboot(a.ip)
    upload(a.ip, a.image)
    if a.node:
        wait_hello(a.node, a.broker)


if __name__ == "__main__":
    main()
