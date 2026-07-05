#!/usr/bin/env bash
# D1001 panel dev-workflow dispatcher — a FIXED, committed, reviewable helper so the recurring
# build/flash/monitor loop runs without per-command permission prompts (allowlist this script by
# exact path: Bash(bash .../tools/panel.sh:*)). Keep its behavior fixed; do NOT turn it into a
# scratch file that gets rewritten per run — that would defeat the permission gate.
#
# Usage: bash tools/panel.sh <build|size|flash|console|pull|reset|port> [args]
#   build            source ESP-IDF env + idf.py build (in place)
#   size             idf.py size (DIRAM/IRAM budget)
#   flash            cable-flash over the stable USB-JTAG by-id symlink (no OTA)
#   console [secs]   follow the console via the by-id symlink, DTR/RTS DEASSERTED (default 60s)
#   pull [host]      force a full replica pull (mosquitto_pub cmd/replica), default broker .210
#   reset            hard-reset a (possibly wedged) panel over the by-id port
#   port             print the resolved by-id serial device
set -euo pipefail

PROJ="/home/visko/ha-coord/provisioning/reterminal/beachhead"
IDF="${IDF_PATH:-$HOME/esp/esp-idf}"
BROKER_DEFAULT="192.168.0.210"
NODE="d1001-beachhead"
BYID_GLOB='/dev/serial/by-id/usb-Espressif_USB_JTAG*-if00'

resolve_port() { for p in $BYID_GLOB; do [ -e "$p" ] && { echo "$p"; return 0; }; done; return 1; }

cmd="${1:-}"; shift || true
case "$cmd" in
  build) . "$IDF/export.sh" >/dev/null 2>&1; exec idf.py -C "$PROJ" build ;;
  size)  . "$IDF/export.sh" >/dev/null 2>&1; exec idf.py -C "$PROJ" size ;;
  port)  resolve_port || { echo "no by-id USB-JTAG device (panel not cabled?)" >&2; exit 1; } ;;
  flash)
    P="$(resolve_port)" || { echo "no by-id device — is the panel cabled to the bench?" >&2; exit 1; }
    . "$IDF/export.sh" >/dev/null 2>&1
    exec idf.py -C "$PROJ" -p "$P" flash ;;
  reset)
    P="$(resolve_port)" || { echo "no by-id device" >&2; exit 1; }
    . "$IDF/export.sh" >/dev/null 2>&1
    exec esptool.py --port "$P" --before default_reset --after hard_reset --chip esp32p4 flash_id ;;
  console)
    SECS="${1:-60}"
    exec python3 - "$SECS" <<'PY'
import glob, sys, time, serial
secs = int(sys.argv[1]); deadline = time.time() + secs
PAT = '/dev/serial/by-id/usb-Espressif_USB_JTAG*-if00'
def find():
    m = glob.glob(PAT); return m[0] if m else None
while time.time() < deadline:
    port = find()
    if not port:
        sys.stdout.write("[cap] no by-id device; retry 1s\n"); sys.stdout.flush(); time.sleep(1); continue
    try:
        s = serial.Serial()
        s.port = port; s.baudrate = 115200; s.timeout = 1
        s.dtr = False; s.rts = False          # USB-serial-JTAG: DTR/RTS drive reset/boot — never assert
        s.open()
        sys.stdout.write(f"[cap] opened {port}\n"); sys.stdout.flush()
        buf = b''
        while time.time() < deadline:
            d = s.read(256)
            if not d: continue
            buf += d
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                sys.stdout.write("[%s] %s\n" % (time.strftime('%H:%M:%S'), line.decode('utf-8','replace')))
                sys.stdout.flush()
    except Exception as e:
        sys.stdout.write(f"[cap] {port} gone ({e}); retry 1s\n"); sys.stdout.flush(); time.sleep(1)
PY
    ;;
  pull)
    H="${1:-$BROKER_DEFAULT}"
    exec mosquitto_pub -h "$H" -t "$NODE/cmd/replica" -m go ;;
  fs)
    # bash tools/panel.sh fs <ls|rm|stat|df|mkdir> [/sdcard/path]
    op="${1:?op required}"; path="${2:-}"
    mosquitto_sub -h "$BROKER_DEFAULT" -t "$NODE/fs" -v -W 8 &
    sub=$!; sleep 0.4
    if [ -n "$path" ]; then msg="{\"op\":\"$op\",\"path\":\"$path\"}"; else msg="{\"op\":\"$op\"}"; fi
    mosquitto_pub -h "$BROKER_DEFAULT" -t "$NODE/cmd/fs" -m "$msg"
    wait "$sub" ;;
  mon)
    # bash tools/panel.sh mon <topic-suffix|#> [secs]  — subscribe to a panel topic
    t="${1:-#}"; secs="${2:-20}"
    exec mosquitto_sub -h "$BROKER_DEFAULT" -t "$NODE/$t" -v -W "$secs" ;;
  *) echo "usage: bash tools/panel.sh <build|size|flash|console|pull|reset|port|fs|mon> [args]" >&2; exit 2 ;;
esac
