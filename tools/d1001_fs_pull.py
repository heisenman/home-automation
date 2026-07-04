#!/usr/bin/env python3
"""Pull a file off the D1001 SD card over MQTT — no card reader, no USB, no firmware change.

Reuses the firmware's already-deployed cmd/fs handler (firmware/components/fs_ops/fs_ops.c): the
`read` op streams any /sdcard file back as base64, <=1536 bytes per chunk, with off/n/size/eof fields.
This driver walks the file chunk-by-chunk (one mosquitto_rr request/response per chunk), base64-decodes
each, and reassembles the whole file locally. Read-only against a live panel => zero wedge risk.

  usage: tools/d1001_fs_pull.py [--broker 192.168.0.210] [--path /sdcard/battprofile.csv] [--out FILE]

Then characterize it:  python3 tools/e1001_profile.py d1001 <out>
"""
import argparse
import base64
import json
import subprocess
import sys

REQ_TOPIC = "d1001-beachhead/cmd/fs"
RES_TOPIC = "d1001-beachhead/fs"
CHUNK = 1536              # == fs_ops READ_CAP; larger len is clamped firmware-side


def rr(broker, req, timeout=6):
    """One request/response round-trip via mosquitto_rr. Returns the parsed JSON reply, or None."""
    p = subprocess.run(
        ["mosquitto_rr", "-h", broker, "-t", REQ_TOPIC, "-e", RES_TOPIC,
         "-W", str(timeout), "-m", json.dumps(req)],
        capture_output=True, text=True)
    for line in reversed(p.stdout.strip().splitlines()):   # last {..} line = freshest reply
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def pull(broker, path, out):
    off, seq, size = 0, 0, None
    data = bytearray()
    while True:
        seq += 1
        req = {"op": "read", "path": path, "off": off, "len": CHUNK, "id": seq}
        r = None
        for _ in range(4):                        # qos0 — retry a dropped/mismatched reply
            r = rr(broker, req)
            if r and r.get("id") == seq and r.get("op") == "read":
                break
            r = None
        if r is None:
            print(f"\nno matching reply at off={off} after retries — is the D1001 online "
                  f"and is the SD mounted? aborting", file=sys.stderr)
            return False
        if not r.get("ok"):
            print(f"\nread failed at off={off}: {r.get('err')}", file=sys.stderr)
            return False
        chunk = base64.b64decode(r.get("b64", ""))
        n = len(chunk)
        size = r.get("size", size)
        data += chunk
        off += n
        if size:
            print(f"\r  {off}/{int(size)} bytes ({100.0 * off / max(size, 1):.0f}%)",
                  end="", file=sys.stderr)
        if r.get("eof") or n == 0:
            break
    with open(out, "wb") as f:
        f.write(data)
    print(f"\n# wrote {len(data)} bytes -> {out}", file=sys.stderr)
    return True


def main():
    ap = argparse.ArgumentParser(description="Pull a D1001 SD file over MQTT (cmd/fs read)")
    ap.add_argument("--broker", default="192.168.0.210")
    ap.add_argument("--path", default="/sdcard/battprofile.csv")
    ap.add_argument("--out")
    a = ap.parse_args()
    out = a.out or a.path.rsplit("/", 1)[-1]
    ok = pull(a.broker, a.path, out)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
