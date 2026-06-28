#!/usr/bin/env bash
# Toggle this host's controllable indicator LEDs for night mode (board led-night-mode).
#
# On a mini-PC the only sysfs-writable LEDs are typically the NIC port LEDs under /sys/class/leds
# (e.g. enp4s0-0::lan). Writing brightness/trigger needs root, so this runs via a narrow NOPASSWD
# sudoers grant (/etc/sudoers.d/ha-host-leds) — the controller (user visko) shells out to it.
#
#   host-leds.sh off  -> save each NIC LED's current (trigger,brightness), then set trigger=none/brightness=0
#   host-leds.sh on   -> restore exactly what 'off' saved (so a box whose LEDs were already off STAYS off —
#                        we never force a steady 255). No saved state (e.g. after a reboot cleared /run):
#                        restore the netdev link/activity trigger if available, else leave as-is.
#
# Only LEDs whose name looks like a NIC port LED (…lan… / …net…) are touched, so this never clobbers a
# keyboard/capslock LED on a divergent box. No-ops cleanly if there are no such LEDs.
set -u
STATE=/run/ha-host-leds.state

is_nic_led() { case "$(basename "$1")" in *lan*|*net*) return 0;; *) return 1;; esac; }
cur_trigger() { grep -oP '\[\K[^\]]+' "$1/trigger" 2>/dev/null; }   # the active trigger (in [brackets])

action="${1:-}"
n=0
case "$action" in
  off)
    mkdir -p "$(dirname "$STATE")"
    for l in /sys/class/leds/*; do
      [ -e "$l/brightness" ] && is_nic_led "$l" || continue
      name=$(basename "$l")
      # save the ORIGINAL state once, so 'on' restores what we actually found (not a forced full brightness)
      if ! grep -q "^$name " "$STATE" 2>/dev/null; then
        echo "$name $(cur_trigger "$l") $(cat "$l/brightness" 2>/dev/null)" >> "$STATE"
      fi
      echo none > "$l/trigger" 2>/dev/null || true
      echo 0 > "$l/brightness" 2>/dev/null || true
      n=$((n+1))
    done
    echo "host NIC LEDs OFF ($n)" ;;
  on)
    for l in /sys/class/leds/*; do
      [ -e "$l/brightness" ] && is_nic_led "$l" || continue
      name=$(basename "$l")
      saved=$(grep "^$name " "$STATE" 2>/dev/null | tail -1)
      if [ -n "$saved" ]; then
        trig=$(echo "$saved" | awk '{print $2}'); br=$(echo "$saved" | awk '{print $3}')
        if [ -n "$trig" ] && [ "$trig" != "none" ]; then echo "$trig" > "$l/trigger" 2>/dev/null || true
        else echo none > "$l/trigger" 2>/dev/null || true; fi
        echo "${br:-0}" > "$l/brightness" 2>/dev/null || true
      elif grep -qw netdev "$l/trigger" 2>/dev/null; then
        echo netdev > "$l/trigger" 2>/dev/null || true        # no saved state -> restore link/activity if we can
      fi
      n=$((n+1))
    done
    rm -f "$STATE"
    echo "host NIC LEDs ON ($n)" ;;
  *) echo "usage: $0 on|off" >&2; exit 2 ;;
esac
