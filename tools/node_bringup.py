#!/usr/bin/env python3
"""
node-bringup — one command to take a *forked* edge-node firmware dir from enrolled → relaying → OTA-proven,
with a hard gate at every stage (FIRMWARE-GUIDE.md §7 promoted to a tool, per ADR-0015 follow-up).

It does NOT write board-specific code: forking a reference node and setting the transport pins / sdkconfig /
gotchas (§3) is human judgement and must already be done. This automates the repeatable, gateable tail:

  1. ENROLL       mint the per-device secret + emit main/secrets.h (broker + OTA host patched in)
  2. BUILD        idf.py build (sets target first if needed)
  3. FLASH        idf.py -p <port> flash            (skip with --skip-flash)
  4. VERIFY-RELAY node reports `online` + ≥1 advert on the broker within --verify-timeout
  5. BENCH-OTA    auto-bump the fw version → rebuild → signed OTA push → require self-test PASS (not rollback),
                  then restore the version line                                  (skip with --skip-bench-ota)

Any stage failing its gate aborts the run (non-zero exit) — "verified", never just "configured".

  . ~/esp/esp-idf/export.sh   # not required (we source it ourselves), but harmless
  python3 tools/node_bringup.py edge/esp32s3-eth s3-crawlspace --mac 28:84:85:54:AB:E0 \
      --target esp32s3 --port /dev/ttyACM0 --broker mqtt://192.168.0.200:1883 \
      --ota-host 192.168.0.200 --serve-ip 192.168.0.210

Notes:
  * Address the dictator by the VIP (.200) so the node follows failover — BUT a Wi-Fi segment that can't ARP
    the VIP must use the box IP (.210). Same caveat for --ota-host (FIRMWARE-GUIDE §3.6).
  * --serve-ip is THIS host's address the node can route to (for the bench-OTA HTTP pull); must equal the
    node's pinned --ota-host, or the firmware host-pin rejects the download.
"""
import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VENV_PY = REPO / "venv" / "bin" / "python3"
DEFAULT_EXPORT = Path.home() / "esp" / "esp-idf" / "export.sh"


def c(s, color):  # tiny ANSI helper (no dependency)
    return f"\033[{color}m{s}\033[0m" if sys.stdout.isatty() else s


def gate(ok, label, detail=""):
    mark = c("PASS", "32") if ok else c("FAIL", "31")
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        print(c(f"\nAborting: gate '{label}' failed.", "31"))
        sys.exit(1)


def run(cmd, **kw):
    print(c(f"  $ {cmd if isinstance(cmd, str) else ' '.join(map(str, cmd))}", "90"))
    return subprocess.run(cmd, **kw)


def idf(board_dir, *args, export):
    """Run idf.py inside the sourced ESP-IDF env (so it works without a pre-sourced shell)."""
    inner = "idf.py -C %s %s" % (board_dir, " ".join(map(str, args)))
    return run(["bash", "-lc", f". {export} >/dev/null 2>&1 && {inner}"])


def parse_define(text, name):
    m = re.search(rf'#define\s+{name}\s+"([^"]*)"', text)
    return m.group(1) if m else None


# ── Stage 1: enroll ────────────────────────────────────────────────────────────────────────────────
def stage_enroll(a, secrets_h):
    print(c("\n[1/5] ENROLL", "1;36"))
    base = a.board_dir / "main" / ("secrets.example.h" if (a.board_dir / "main/secrets.example.h").exists()
                                   else "secrets.h")
    cmd = [str(VENV_PY), str(REPO / "tools/enroll_node.py"), "--node-id", a.node_id,
           "--mac", a.mac, "--out", str(secrets_h), "--base-secrets", str(base)]
    if a.rotate:
        cmd.append("--rotate")
    if a.master:
        cmd += ["--master", a.master]
    r = run(cmd)
    gate(r.returncode == 0 and secrets_h.exists(), "enroll wrote secrets.h")

    # Patch broker + OTA host (enroll inherits broker from base; it has no --broker/--ota-host).
    txt = secrets_h.read_text()
    txt = re.sub(r'(#define\s+HA_BROKER_URI\s+)"[^"]*"', rf'\1"{a.broker}"', txt)
    if a.ota_host is not None:
        if "HA_OTA_HOST" in txt:
            txt = re.sub(r'(#define\s+HA_OTA_HOST\s+)"[^"]*"', rf'\1"{a.ota_host}"', txt)
        else:
            txt += f'#define HA_OTA_HOST     "{a.ota_host}"\n'
    secrets_h.write_text(txt)

    secret = parse_define(txt, "HA_CMD_SECRET")
    gate(bool(secret), "HA_CMD_SECRET provisioned (else node rejects every signed command)")
    gate(parse_define(txt, "HA_BROKER_URI") == a.broker, "broker pinned", a.broker)
    if a.ota_host is not None:
        gate(parse_define(txt, "HA_OTA_HOST") == a.ota_host, "OTA host pinned", a.ota_host)
    return secret


# ── Stage 2: build ─────────────────────────────────────────────────────────────────────────────────
def stage_build(a):
    print(c("\n[2/5] BUILD", "1;36"))
    if not (a.board_dir / "sdkconfig").exists() or a.set_target:
        gate(idf(a.board_dir, "set-target", a.target, export=a.export).returncode == 0,
             "set-target", a.target)
    gate(idf(a.board_dir, "build", export=a.export).returncode == 0, "idf.py build")
    bins = list((a.board_dir / "build").glob("*.bin"))
    app = next((b for b in bins if b.name not in ("bootloader.bin", "partition-table.bin",
                                                  "ota_data_initial.bin")), None)
    gate(app is not None, "app .bin produced", app.name if app else "none")
    return app


# ── Stage 3: flash ─────────────────────────────────────────────────────────────────────────────────
def stage_flash(a):
    print(c("\n[3/5] FLASH", "1;36"))
    gate(idf(a.board_dir, "-p", a.port, "flash", export=a.export).returncode == 0,
         "idf.py flash", a.port)


# ── Stage 4: verify-relay ──────────────────────────────────────────────────────────────────────────
def stage_verify(a):
    print(c("\n[4/5] VERIFY-RELAY", "1;36"))
    import paho.mqtt.client as mqtt
    state = {"online": False, "advert": False}

    def on_connect(cl, u, f, rc, props=None):
        cl.subscribe(f"home/edge/{a.node_id}/status", 1)
        cl.subscribe(f"home/edge/{a.node_id}/+/adv", 1)

    def on_message(cl, u, msg):
        p = msg.payload.decode(errors="replace")
        if msg.topic.endswith("/status") and p.startswith("online"):
            print(f"  status: {p}"); state["online"] = True
        elif msg.topic.endswith("/adv"):
            if not state["advert"]:
                print(f"  advert: {msg.topic.split('/')[-2]} …")
            state["advert"] = True

    cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    cl.on_connect = on_connect
    cl.on_message = on_message
    if os.environ.get("HA_MQTT_USER"):
        cl.username_pw_set(os.environ["HA_MQTT_USER"], os.environ.get("HA_MQTT_PASS"))
    host = re.sub(r"^mqtt://", "", a.broker).split(":")[0]
    cl.connect(host, a.broker_port, 30)
    cl.loop_start()
    print(f"  waiting up to {a.verify_timeout}s for online + an advert on {host} …")
    t0 = time.time()
    while time.time() - t0 < a.verify_timeout and not (state["online"] and state["advert"]):
        time.sleep(0.5)
    cl.loop_stop(); cl.disconnect()
    gate(state["online"], "node reported online")
    gate(state["advert"], "node relayed ≥1 advert")


# ── Stage 5: bench-OTA ─────────────────────────────────────────────────────────────────────────────
def stage_bench_ota(a, secret):
    print(c("\n[5/5] BENCH-OTA", "1;36"))
    if not a.serve_ip:
        gate(False, "--serve-ip required for bench-OTA (or pass --skip-bench-ota)")
    vfile = a.board_dir / a.version_file
    orig = vfile.read_text()
    m = re.search(r'#define\s+HA_FW_VERSION\s+"([^"]*)"', orig)
    gate(m is not None, f"HA_FW_VERSION found in {a.version_file}")
    bumped = (m.group(1) + "-ota") if not m.group(1).endswith("-ota") else (m.group(1) + "x")
    print(f"  bumping HA_FW_VERSION {m.group(1)} → {bumped} for the OTA test image")
    vfile.write_text(orig[:m.start(1)] + bumped + orig[m.end(1):])
    try:
        app = stage_build(a)  # rebuild the bumped image (reuses the gated build stage)
        env = dict(os.environ, HA_CMD_SECRET=secret)
        r = run([str(VENV_PY), str(REPO / "tools/edge_ota.py"), "--node", a.node_id,
                 "--bin", str(app), "--serve-ip", a.serve_ip,
                 "--broker", re.sub(r"^mqtt://", "", a.broker).split(":")[0],
                 "--broker-port", str(a.broker_port)], env=env)
        gate(r.returncode == 0, "OTA self-test PASS (new slot confirmed, not rolled back)")
    finally:
        vfile.write_text(orig)
        print(c(f"  restored {a.version_file} (version line back to {m.group(1)})", "90"))


def main():
    p = argparse.ArgumentParser(description="Bring up an edge node end-to-end with a gate at each stage")
    p.add_argument("board_dir", type=Path, help="forked node dir, e.g. edge/esp32s3-eth")
    p.add_argument("node_id")
    p.add_argument("--mac", default="", help="chip eFuse MAC (esptool read_mac) — recorded in the LUT")
    p.add_argument("--target", default="esp32s3", help="esp32s3 | esp32c6 | esp32c3 | …")
    p.add_argument("--port", default="/dev/ttyACM0")
    p.add_argument("--broker", default="mqtt://192.168.0.200:1883",
                   help="VIP by default; use the box IP on a segment that can't reach the VIP")
    p.add_argument("--broker-port", type=int, default=1883)
    p.add_argument("--ota-host", default=None, help="OTA host pin (default: match the broker host)")
    p.add_argument("--serve-ip", default=None, help="this host's IP the node can pull the bench-OTA bin from")
    p.add_argument("--ntp", default=None)
    p.add_argument("--master", default=None, help="master passphrase (else instance/.master_pass)")
    p.add_argument("--version-file", default="main/ha_mqtt.c", help="file holding #define HA_FW_VERSION")
    p.add_argument("--verify-timeout", type=float, default=90.0)
    p.add_argument("--export", default=str(DEFAULT_EXPORT), help="path to ESP-IDF export.sh")
    p.add_argument("--set-target", action="store_true", help="force idf.py set-target (wipes sdkconfig)")
    p.add_argument("--rotate", action="store_true", help="re-enroll an already-enrolled node (new secret)")
    p.add_argument("--skip-enroll", action="store_true",
                   help="reuse the existing main/secrets.h (rebuild/reflash without re-minting the secret)")
    p.add_argument("--skip-flash", action="store_true")
    p.add_argument("--skip-bench-ota", action="store_true")
    a = p.parse_args()

    a.board_dir = (REPO / a.board_dir).resolve() if not a.board_dir.is_absolute() else a.board_dir
    if a.ota_host is None:
        a.ota_host = re.sub(r"^mqtt://", "", a.broker).split(":")[0]
    if not (a.board_dir / "main").is_dir():
        sys.exit(f"{a.board_dir} has no main/ — pass a forked node dir (see FIRMWARE-GUIDE §7 steps 1–2,4)")
    print(c(f"node-bringup: {a.node_id} in {a.board_dir.relative_to(REPO)} "
            f"(target={a.target}, broker={a.broker}, ota-host={a.ota_host})", "1"))

    secrets_h = a.board_dir / "main" / "secrets.h"
    if a.skip_enroll:
        print(c("\n[1/5] ENROLL — skipped (--skip-enroll); reusing main/secrets.h", "33"))
        gate(secrets_h.exists(), "existing secrets.h present")
        secret = parse_define(secrets_h.read_text(), "HA_CMD_SECRET")
        gate(bool(secret), "HA_CMD_SECRET present in existing secrets.h")
    else:
        secret = stage_enroll(a, secrets_h)
    stage_build(a)
    if not a.skip_flash:
        stage_flash(a)
    else:
        print(c("\n[3/5] FLASH — skipped (--skip-flash)", "33"))
    stage_verify(a)
    if not a.skip_bench_ota:
        stage_bench_ota(a, secret)
    else:
        print(c("\n[5/5] BENCH-OTA — skipped (--skip-bench-ota)", "33"))

    print(c(f"\n✓ {a.node_id} brought up: enrolled → built → "
            f"{'flashed → ' if not a.skip_flash else ''}relaying"
            f"{' → OTA-proven' if not a.skip_bench_ota else ''}.", "1;32"))


if __name__ == "__main__":
    main()
