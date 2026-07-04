#!/usr/bin/env bash
# E1001 battery-profiler capture (Module 3, front-end). Robustly logs the battprofile telemetry stream to a
# timestamped JSONL for an overnight profiling run — each line = "<epoch_seconds> <json>". mosquitto_sub is
# wrapped in a reconnect loop so a transient broker/network blip doesn't end the capture. Analyze with
# tools/e1001_profile.py fit <file>.
#   usage: tools/e1001_capture.sh [broker] [outfile]
set -u
BROKER="${1:-192.168.0.210}"
OUT="${2:-$HOME/e1001-battprofile-$(date +%Y%m%d-%H%M).jsonl}"
echo "capturing e1001-bench/battprofile from $BROKER -> $OUT" >&2
while true; do
  stdbuf -oL mosquitto_sub -h "$BROKER" -t 'e1001-bench/battprofile' 2>/dev/null | while IFS= read -r line; do
    printf '%s %s\n' "$(date +%s)" "$line" >> "$OUT"
  done
  printf '%s # mosquitto_sub dropped — reconnecting in 5s\n' "$(date +%s)" >> "$OUT"
  sleep 5
done
