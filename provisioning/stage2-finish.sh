#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Stage 2b — post-install finisher.  RUN ONCE, as the 'visko' user, right after
# your first SSH login on a freshly bootstrapped box.  NO Claude session needed.
#
#     cd ~/home_automation
#     ./provisioning/stage2-finish.sh
#
# This automates the parts of provisioning/02-full-server-spec.md (§4–§7) that the
# unattended install + firstboot.sh do NOT cover, i.e. everything that was done by
# hand when ha-dev was brought up on 2026-06-24:
#
#     §4  full apt package set        (bluez, bluetooth, mosquitto, ethtool, rsync, …)
#     §5  BlueZ --experimental        (drop-in so the scanner's passive or_patterns work)
#     §6/§7  venv + app + services     (via install.sh, run as YOU not root — see footgun below)
#     +   persistent journald          (so an overnight/first-night log survives a reboot)
#     +   verification gates           (services active, BLE adverts flowing)
#
# It is IDEMPOTENT — safe to run repeatedly; every step checks before it changes.
#
# It deliberately does NOT do the steps that need a human present, drop your SSH
# session, or touch PII — those are PRINTED at the end with exact copy-paste
# commands.  Two of them can be done by this script via opt-in flags:
#
#     --narrow-sudoers   Replace the broad bootstrap sudoers grant with the narrow
#                        ha-services rule (spec §7e).  Requires a console password
#                        to be set first (sudo passwd visko) or you can lock yourself
#                        out of sudo — the script refuses if no password is set.
#
#     --help             Show this header.
#
# The genuinely manual finishers (static-IP cutover, password, sneakernet
# devices.yaml/weather.env, reboot test) are summarised at the end every run.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Resolve paths & sanity-check how we were invoked ──────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_USER="$(id -un)"

c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_red=$'\033[31m'; c_bld=$'\033[1m'; c_rst=$'\033[0m'
step(){ printf '\n%s━━ %s%s\n' "$c_bld" "$*" "$c_rst"; }
ok(){   printf '   %s✓%s %s\n' "$c_grn" "$c_rst" "$*"; }
skip(){ printf '   %s•%s %s\n' "$c_yel" "$c_rst" "$*"; }
warn(){ printf '   %s!%s %s\n' "$c_yel" "$c_rst" "$*"; }
die(){  printf '\n%s✗ %s%s\n' "$c_red" "$*" "$c_rst" >&2; exit 1; }

NARROW_SUDOERS=0
for arg in "$@"; do
  case "$arg" in
    --narrow-sudoers) NARROW_SUDOERS=1 ;;
    -h|--help) sed -n '2,46p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $arg  (try --help)" ;;
  esac
done

# The #1 footgun the spec warns about: running install.sh as root makes venv/ and
# instance/ root-owned, and the User=visko services then fail to open the DB.
# This whole script self-elevates with internal sudo, so it must NOT be run as root.
[[ $EUID -eq 0 ]] && die "Do NOT run this as root / with sudo. Run it as your normal user:  ./provisioning/stage2-finish.sh  (it uses sudo internally where needed)."
[[ -f "$REPO_DIR/install.sh" ]] || die "Can't find install.sh — run this from inside the repo (cd ~/home_automation)."

step "Stage 2b finisher — host: $(hostname), user: $RUN_USER, repo: $REPO_DIR"
ok "$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME") · kernel $(uname -r) · python $(python3 --version 2>&1 | awk '{print $2}')"
# Warm the sudo cache once up front so prompts (if any) happen here, not mid-step.
sudo -v || die "sudo is required."

# ── §4  Full system package set (superset of spec §4 + install.sh + firstboot) ─
step "§4  System packages"
PKGS=(git curl ca-certificates build-essential pkg-config
      python3 python3-venv python3-dev python3-pip
      mosquitto mosquitto-clients
      bluez bluetooth libdbus-1-dev
      ethtool rsync)
missing=()
for p in "${PKGS[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
if ((${#missing[@]})); then
  warn "installing: ${missing[*]}"
  sudo apt-get update -q
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
  ok "packages installed"
else
  skip "all ${#PKGS[@]} packages already present"
fi

# ── §5  BlueZ experimental (passive-scan or_patterns the scanner relies on) ────
step "§5  BlueZ --experimental drop-in"
BT_BIN="$(systemctl cat bluetooth 2>/dev/null | sed -n 's/^ExecStart=\([^ ]*bluetoothd\).*/\1/p' | head -1)"
BT_BIN="${BT_BIN:-/usr/libexec/bluetooth/bluetoothd}"
DROPIN_DIR=/etc/systemd/system/bluetooth.service.d
DROPIN="$DROPIN_DIR/experimental.conf"
WANT="$(printf '[Service]\nExecStart=\nExecStart=%s --experimental\n' "$BT_BIN")"
if [[ -f "$DROPIN" ]] && diff -q <(printf '%s\n' "$WANT") "$DROPIN" >/dev/null 2>&1; then
  skip "experimental drop-in already in place ($BT_BIN)"
else
  sudo mkdir -p "$DROPIN_DIR"
  printf '%s\n' "$WANT" | sudo tee "$DROPIN" >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl restart bluetooth
  ok "installed + restarted bluetooth ($BT_BIN --experimental)"
fi
sudo systemctl enable bluetooth >/dev/null 2>&1 || true
if systemctl show bluetooth -p ExecStart | grep -q experimental; then
  ok "bluetoothd running with --experimental"
else
  warn "bluetoothd does not show --experimental yet — check 'systemctl status bluetooth'"
fi

# ── Persistent journald (so the first night's logs survive a reboot) ──────────
step "Persistent journald"
if [[ -d /var/log/journal ]]; then
  skip "/var/log/journal already exists (persistent)"
else
  sudo mkdir -p /var/log/journal
  sudo systemd-tmpfiles --create --prefix /var/log/journal >/dev/null 2>&1 || true
  sudo systemctl kill --kill-who=main --signal=SIGUSR1 systemd-journald 2>/dev/null || true
  ok "enabled persistent journald"
fi

# ── §6/§7  App: venv, config, mosquitto, systemd units — via install.sh ───────
# install.sh is itself idempotent and self-elevates; we run it AS THE USER so the
# venv/ and instance/ it creates stay user-owned (the root-owned-venv footgun).
step "§6/§7  App install (install.sh — venv, mosquitto, ha-* services)"
( cd "$REPO_DIR" && ./install.sh )
# Safety net for a box where install.sh was previously mis-run as root.
if [[ -e "$REPO_DIR/venv" || -e "$REPO_DIR/instance" ]]; then
  owner="$(stat -c '%U' "$REPO_DIR/venv" 2>/dev/null || echo "$RUN_USER")"
  if [[ "$owner" != "$RUN_USER" ]]; then
    warn "venv/instance were owned by '$owner' — fixing to '$RUN_USER'"
    sudo chown -R "$RUN_USER:$RUN_USER" "$REPO_DIR/venv" "$REPO_DIR/instance"
    sudo systemctl restart ha-writer.service ha-api.service 2>/dev/null || true
    ok "ownership corrected"
  else
    ok "venv/instance owned by $RUN_USER"
  fi
fi

# ── BLE radio report (dongle vs onboard — informational, no change) ───────────
step "BLE radio"
if command -v bluetoothctl >/dev/null && bluetoothctl list 2>/dev/null | grep -q Controller; then
  bluetoothctl list 2>/dev/null | sed 's/^/   /'
  if lsusb 2>/dev/null | grep -qiE '0bda:(8771|a771)|RTL8761'; then
    ok "TP-Link UB500 (RTL8761B) dongle detected — the intended BLE radio"
  else
    warn "No UB500 dongle detected; scanner will use the onboard adapter."
    warn "On ha-dev the onboard MediaTek BT worked for passive scanning, but it is the"
    warn "known-risk radio — fit the UB500 if you see scanner watchdog restarts/stalls."
  fi
else
  warn "No BLE controller is up yet. Plug in the UB500 (or check 'rfkill list')."
fi

# ── §7e  (optional) narrow sudoers ────────────────────────────────────────────
if (( NARROW_SUDOERS )); then
  step "§7e  Narrow sudoers (replace broad bootstrap grant)"
  if ! sudo passwd -S "$RUN_USER" 2>/dev/null | awk '{print $2}' | grep -q '^P$'; then
    die "No console password is set for '$RUN_USER'. Set one first:  sudo passwd $RUN_USER  — otherwise removing the NOPASSWD-ALL grant locks you out of sudo."
  fi
  sudo tee /etc/sudoers.d/ha-services >/dev/null <<EOF
$RUN_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl start ha-*, /usr/bin/systemctl stop ha-*, \\
 /usr/bin/systemctl restart ha-*, /usr/bin/systemctl enable ha-*, /usr/bin/systemctl disable ha-*, \\
 /usr/bin/systemctl daemon-reload, /usr/bin/systemctl start mosquitto, \\
 /usr/bin/systemctl stop mosquitto, /usr/bin/systemctl restart mosquitto
EOF
  sudo chmod 440 /etc/sudoers.d/ha-services
  if sudo visudo -cf /etc/sudoers.d/ha-services >/dev/null; then
    sudo rm -f /etc/sudoers.d/90-visko-bootstrap
    ok "ha-services sudoers installed; broad bootstrap grant removed"
  else
    sudo rm -f /etc/sudoers.d/ha-services
    die "visudo syntax check FAILED — reverted, left bootstrap grant in place."
  fi
fi

# ── Verification gates (spec §9, the automatable subset) ───────────────────────
step "Verify"
CORE=(ha-writer ha-api ha-edge-mapper ha-edge-history mosquitto bluetooth)
bad=0
for s in "${CORE[@]}"; do
  if [[ "$(systemctl is-active "$s" 2>/dev/null)" == active ]]; then ok "$s active"; else warn "$s NOT active"; bad=1; fi
done
# scanner is separate: it needs a working radio
if [[ "$(systemctl is-active ha-scanner 2>/dev/null)" == active ]]; then
  ok "ha-scanner active (NRestarts=$(systemctl show ha-scanner -p NRestarts --value 2>/dev/null))"
else
  warn "ha-scanner not active — usually means no BLE adapter; see BLE section above"
fi
# best-effort: are adverts arriving? (short, non-fatal)
if command -v mosquitto_sub >/dev/null; then
  n="$(timeout 8 mosquitto_sub -h localhost -t 'home/#' -W 6 2>/dev/null | grep -c . || true)"
  [[ "${n:-0}" -gt 0 ]] && ok "MQTT live: $n message(s) on home/# in 6s" || warn "no MQTT messages in 6s (fine if no sensors are in range yet)"
fi
controller_state="$(systemctl is-active ha-controller 2>/dev/null || true)"
[[ "$controller_state" == active ]] && warn "ha-controller is ACTIVE — on a dev box it must stay OFF so it doesn't fight the live .245 loop" || ok "ha-controller off (correct for a non-dictator box)"

# ── Remaining human-only steps ────────────────────────────────────────────────
step "${c_bld}DONE — remaining manual steps (need a human / drop SSH / touch PII)${c_rst}"
cat <<EOF
   These are intentionally NOT automated. Do them when you're ready:

   1. PERMANENT STATIC IP (drops your SSH session — do it on console or be ready to
      reconnect at the new address). This box was bootstrapped with ifupdown
      (/etc/network/interfaces). Edit the address there, then 'sudo reboot' (cleanest)
      or 'sudo systemctl restart networking'. ha-dev's chosen address is 192.168.0.210.

   2. CONSOLE PASSWORD + NARROW SUDOERS:
          sudo passwd $RUN_USER
          ./provisioning/stage2-finish.sh --narrow-sudoers
      (Order matters: set the password FIRST, or removing the NOPASSWD-ALL grant
      locks you out of sudo.)

   3. REAL DEVICE REGISTRY (PII — sneakernet only, never git):
      copy instance/devices.yaml (+ instance/weather.env) from your USB / .245 into
      $REPO_DIR/instance/ . Until then meters publish to home/unknown/<mac>/raw.
          sudo systemctl restart ha-writer ha-scanner    # after copying

   4. REBOOT TEST (spec §9): sudo reboot; confirm all ha-* services + scanner return.

   Prefer to drive interactively with Claude instead? See provisioning/04-post-install.md
   ("Drive with Claude") — it doubles as the on-device LLM directive.
EOF
(( bad )) && warn "Some core services were not active — investigate before relying on this box." || ok "Core stack is up."
