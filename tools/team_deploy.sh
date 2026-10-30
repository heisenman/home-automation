#!/usr/bin/env bash
# Deploy gateway for the shared checkout: lets Catherine's account (and its Claude) push, deploy the PWA to
# ha-2, and OTA a D1001 panel — the steps that need visko's GitHub login, ha-2 ssh key and panel command
# secret. Hugh, 2026-10-30: "it's fine for Catherine's dev to deploy as they work together on updates."
#
#   sudo -u visko /home/visko/home_automation/tools/team_deploy.sh push
#   sudo -u visko /home/visko/home_automation/tools/team_deploy.sh pwa <commit|A..B>
#   sudo -u visko /home/visko/home_automation/tools/team_deploy.sh d1001 <commit> <d1001_2|d1001-beachhead>
#
# push   git push origin main (fast-forward only; refuses if origin has commits this checkout lacks).
# pwa    push, then tools/ha2_deploy.sh <commit> ha-api ha-api-tls. Only for commits that touch the PWA/BFF
#        (server/web/, server/api/) plus tests/ and docs/ — anything else is a visko-side deploy.
# d1001  push, build provisioning/reterminal/beachhead as of <commit> (the checkout's copy of that folder must
#        match the commit), serve the bin from ha-2, signed OTA to ONE panel, wait until it reports the new
#        build with the display on. One panel per run so a person can check the reboot before the next.
#        Panel 2 (d1001_2) is usually first. Rollback = run again with the previous commit.
# Every run ends with a one-line post to the host coordination board (seat "deploy").
# Sudoers rule (one line, /etc/sudoers.d/ha-team-deploy):
#   catherine ALL=(visko) NOPASSWD: /home/visko/home_automation/tools/team_deploy.sh *
set -euo pipefail

[ "$(id -un)" = visko ] || { echo "run as visko: sudo -u visko $0 $*"; exit 2; }
export HOME=/home/visko
REPO=/home/visko/home_automation
HA2=192.168.1.200
FW=provisioning/reterminal/beachhead
cd "$REPO"

who="${SUDO_USER:-visko}"
board() { ~/.claude/host/bin/coord-local.py --as deploy post "[$who] $*" >/dev/null 2>&1 || true; }
die() { echo "FAILED: $*"; board "FAILED: $*"; exit 1; }

push() {
  git fetch -q origin main || die "git fetch failed"
  git merge-base --is-ancestor origin/main HEAD \
    || die "origin/main has commits this checkout lacks — a visko-side session must integrate them first"
  if [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ]; then
    echo "== origin/main already at $(git log --oneline -1)"
  else
    git push -q origin HEAD:main || die "git push failed"
    echo "== pushed: origin/main = $(git log --oneline -1)"
  fi
}

reachable() {  # the commit must be on main (so it is pushed and anyone can rebuild it)
  local c="${1##*..}"
  git rev-parse --verify --quiet "$c^{commit}" >/dev/null || die "'$c' is not a commit here"
  git merge-base --is-ancestor "$c" HEAD || die "$c is not on main (commit it first)"
}

pwa() {
  local spec="$1" base tip
  reachable "$spec"
  if [[ "$spec" == *..* ]]; then base="${spec%%..*}"; tip="${spec##*..}"; else base="$spec^"; tip="$spec"; fi
  local bad
  bad=$(git diff --name-only "$base" "$tip" | grep -Ev '^(server/web/|server/api/|tests/|docs/)' || true)
  [ -z "$bad" ] || die "pwa deploy of $spec refused — it touches files outside the PWA/BFF: $(echo $bad)"
  push
  tools/ha2_deploy.sh "$spec" ha-api ha-api-tls || die "ha2_deploy $spec failed"
  board "PWA $spec deployed to ha-2 (ha-api, ha-api-tls restarted): $(git log --oneline -1 "$tip")"
}

status() { timeout 6 mosquitto_sub -h "$HA2" -t "$1/status" -C 1 -W 5 2>/dev/null || true; }

d1001() {
  local c="$1" panel="$2"
  case "$panel" in d1001_2|d1001-beachhead) ;; *) die "panel must be d1001_2 or d1001-beachhead";; esac
  reachable "$c"
  git diff --quiet "$c" -- "$FW" || die "$FW in the checkout differs from $c (uncommitted edits?) — commit, then deploy"
  local tag
  tag=$(git show "$c:$FW/main/beachhead_main.c" | sed -n 's/^#define APP_BUILD_TAG "\(.*\)"/\1/p')
  [ -n "$tag" ] || die "no APP_BUILD_TAG in $c"
  push
  echo "== building $tag ($c)"
  ( set +u; source ~/esp/esp-idf/export.sh >/dev/null; idf.py -C "$FW" build >/tmp/team_deploy_build.log 2>&1 ) \
    || die "build failed — see /tmp/team_deploy_build.log"
  local bin="d1001_${tag}.bin" port=$((8090 + RANDOM % 10))
  scp -q "$FW/build/d1001_beachhead.bin" "$HA2:ota-canary/$bin" || die "copy to ha-2 failed"
  ssh "$HA2" "cd ~/ota-canary && timeout 300 python3 -m http.server $port --bind 192.168.1.210" >/dev/null 2>&1 &
  SRV=$!
  trap 'kill $SRV 2>/dev/null || true' EXIT
  sleep 3
  echo "== OTA $panel <- http://192.168.1.210:$port/$bin"
  HA_BROKER=$HA2 HA_PANEL=$panel venv/bin/python tools/d1001_cmd.py ota "http://192.168.1.210:$port/$bin" \
    || die "OTA command to $panel failed"
  local s=""
  for _ in $(seq 1 30); do  # up to ~4 min: download + write + reboot + reconnect
    sleep 8
    s=$(status "$panel")
    if echo "$s" | grep -q "\"build\":\"$tag\"" && echo "$s" | grep -q '"display":true'; then
      echo "== $panel is up on $tag, display on"
      board "D1001 $tag ($c) OTA'd to $panel — boots, display on"
      return 0
    fi
  done
  die "$panel did not report $tag with display on within 4 min (last status: ${s:-none}) — check the panel; rollback = this tool with the previous commit"
}

case "${1:-}" in
  push)  push; board "pushed: origin/main = $(git log --oneline -1)";;
  pwa)   [ $# -eq 2 ] || { sed -n 5,8p "$0"; exit 2; }; pwa "$2";;
  d1001) [ $# -eq 3 ] || { sed -n 5,8p "$0"; exit 2; }; d1001 "$2" "$3";;
  *)     sed -n 2,8p "$0"; exit 2;;
esac
