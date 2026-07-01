#!/usr/bin/env bash
# Best-effort full-flash image of an Espressif device that tolerates unreadable/marginal
# sectors: reads coarse blocks, and for any block that won't read, subdivides to a fine
# granularity and zero-fills only the sectors that fail. Result: a full-size image,
# complete except documented gaps (logged to <workdir>/gaps.txt).
#
# Proven on the reTerminal D1001 (ESP32-P4, 32 MB) 2026-07-01, where one 64 KB sector at
# 0x600000 was unreadable — this captured the other 99.8 %. See README.md.
#
# Why these settings (see README "reusable lessons"): use the STUB (not --no-stub — the ROM
# loader is slower and LESS stable for long reads); small chunks + per-chunk auto-reset so a
# failure is cheap and recoverable; hard-reset between chunks (RTS) is the clean recovery path.
#
# Usage:
#   PORT=/dev/ttyACM0 SIZE=$((32*1024*1024)) ./resilient-flash-backup.sh ~/dev-factory-backup.bin
# Env (all optional): PORT (def /dev/ttyACM0), SIZE bytes (def 32MB), BLK coarse (def 1MB),
#   FINE (def 64KB), ESPTOOL (def ~/.flashtools/bin/esptool), WORK (def <out-dir>/.flash-blocks).
# Keep the resulting image OFF-GIT (vendor firmware may carry creds/calibration).
set -u
OUT="${1:?usage: resilient-flash-backup.sh <output.bin>}"
ESPTOOL="${ESPTOOL:-$HOME/.flashtools/bin/esptool}"
PORT="${PORT:-/dev/ttyACM0}"
SIZE="${SIZE:-$((32*1024*1024))}"
BLK="${BLK:-$((1024*1024))}"     # coarse read (fast path)
FINE="${FINE:-$((64*1024))}"     # fine read (around bad sectors)
WORK="${WORK:-$(dirname "$OUT")/.flash-blocks}"
GAPLOG="$(dirname "$OUT")/gaps.txt"
rm -rf "$WORK"; mkdir -p "$WORK"; : > "$GAPLOG"

read_at() {  # addr size outfile -> 0 ok / 1 fail
  local addr=$1 size=$2 of=$3 t
  for t in 1 2; do
    "$ESPTOOL" --port "$PORT" --before default-reset --after hard-reset \
      read-flash "$addr" "$size" "$of" >/dev/null 2>&1 \
      && [ "$(stat -c%s "$of" 2>/dev/null)" = "$size" ] && return 0
    sleep 1
  done
  return 1
}

addr=0
while [ "$addr" -lt "$SIZE" ]; do
  of="$WORK/$(printf '%010x' "$addr").bin"
  if read_at "$addr" "$BLK" "$of"; then
    echo "ok    $(printf '0x%08x' "$addr")  $((BLK/1024))K"
  else
    echo "COARSE FAIL $(printf '0x%08x' "$addr") -> subdividing to $((FINE/1024))K"
    faddr="$addr"; fend=$((addr+BLK)); : > "$of"
    while [ "$faddr" -lt "$fend" ]; do
      fof="$WORK/f_$(printf '%010x' "$faddr").bin"
      if read_at "$faddr" "$FINE" "$fof"; then
        cat "$fof" >> "$of"
      else
        echo "  GAP $(printf '0x%08x' "$faddr") $((FINE/1024))K (zero-filled)" | tee -a "$GAPLOG"
        head -c "$FINE" /dev/zero >> "$of"
      fi
      rm -f "$fof"
      faddr=$((faddr+FINE))
    done
  fi
  addr=$((addr+BLK))
done

cat "$WORK"/??????????.bin > "$OUT"
rm -rf "$WORK"
echo "=== assembled $(stat -c%s "$OUT") bytes (expect $SIZE) ==="
echo "=== gaps (unreadable, zero-filled) ==="; cat "$GAPLOG" 2>/dev/null; [ -s "$GAPLOG" ] || echo "  none"
