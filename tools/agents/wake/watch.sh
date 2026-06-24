#!/usr/bin/env bash
# Interrupt-driven wake watcher (one per agent, on a box that HAS the claude CLI — today only 210).
# Blocks for free on ha/agents/wake/<self>; on a real wake it invokes a headless `claude -p` runner
# scoped by POLICY.md. Idle cost = zero (no LLM until a wake lands). Kill switch: stop this unit.
#
#   env: HA_AGENT_ID (ops|dev)  HA_COORD_BROKER(=VIP)  CLAUDE_BIN  WAKE_DEBOUNCE  WAKE_COOLDOWN  WAKE_DRY_RUN
set -uo pipefail
AGENT="${HA_AGENT_ID:-dev}"
BROKER="${HA_COORD_BROKER:-192.168.0.200}"
PORT="${HA_COORD_PORT:-1883}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
DEBOUNCE="${WAKE_DEBOUNCE:-4}"      # let a burst of bus updates settle into ONE invocation
COOLDOWN="${WAKE_COOLDOWN:-30}"     # min seconds between runner invocations (anti-storm / anti-pingpong)
DRY_RUN="${WAKE_DRY_RUN:-0}"
LOG="${WAKE_LOG:-$REPO/instance/wake-activity.log}"
PROMPT="$HERE/runner-prompt.md"
TOPIC="ha/agents/wake/$AGENT"
log(){ printf '%s watch[%s] %s\n' "$(date -Is)" "$AGENT" "$*" | tee -a "$LOG" 2>/dev/null; }

mkdir -p "$(dirname "$LOG")"
if [ "$DRY_RUN" != 1 ] && ! command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
  log "FATAL: claude CLI ('$CLAUDE_BIN') not found — a wake watcher must run on a box with the CLI (e.g. 210). Exiting."
  exit 1
fi
log "watcher up (broker=$BROKER topic=$TOPIC debounce=${DEBOUNCE}s cooldown=${COOLDOWN}s dry=$DRY_RUN repo=$REPO)"

last=0
mosquitto_sub -h "$BROKER" -p "$PORT" -t "$TOPIC" 2>/dev/null | while IFS= read -r msg; do
  nowts=$(date +%s)
  if [ "$(( nowts - last ))" -lt "$COOLDOWN" ]; then
    log "wake during cooldown (${COOLDOWN}s) — coalesced: $msg"; continue
  fi
  log "WAKE: $msg"
  sleep "$DEBOUNCE"
  last=$(date +%s)
  if [ "$DRY_RUN" = 1 ]; then log "DRY_RUN -> would invoke runner now"; continue; fi
  log "invoking headless runner…"
  # The prompt is passed via -p, so the runner needs NO stdin. We're inside a `mosquitto_sub | while read`
  # loop, so claude's stdin is the bus pipe — without `< /dev/null` it blocks ~3s waiting for stdin
  # ("no stdin data received in 3s" warning) AND could consume a queued wake message off the pipe.
  # Detach it: feed the runner /dev/null, leave the pipe for the loop's `read`.
  ( cd "$REPO" && HA_AGENT_ID="$AGENT" "$CLAUDE_BIN" -p \
        "$(cat "$PROMPT")

WAKE SIGNAL PAYLOAD: $msg" \
        --allowedTools "Bash Read Edit Write" </dev/null >>"$LOG" 2>&1 ) \
    && log "runner finished ok" || log "runner exited nonzero (see log)"
done
log "subscription ended (broker down?) — systemd will restart"
