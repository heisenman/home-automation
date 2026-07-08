#!/usr/bin/env bash
# provisioning/airgap/preflight-readiness.sh — Phase -1 gate for the air-gap migration.
#
# Run ON .210 (the control plane). Asserts ha-2 is fully pre-provisioned AND reachable over the exact
# id_cluster channel we'll use post-gap, WHILE we still have guaranteed household access as the fallback.
# See docs/airgap/MIGRATION-DESIGN-LOG.md (DJ-12, Phase -1).
#
# Two tiers:
#   TIER 1 (Phase -1 gate) — access hooks, offline deps, base, services, secrets. MUST be green before
#                            ha-2 changes networks (the "point of no easy return").
#   TIER 2 (later phases)  — migration tooling deployed + full :import dry-run. Reported PENDING here;
#                            they ride the id_cluster SSH channel later (no internet needed on ha-2).
#
# Usage: provisioning/airgap/preflight-readiness.sh [ha2_host]     (default: 192.168.0.238 household;
#        after the move, pass 192.168.1.210)
set -uo pipefail
HA2="${1:-${HA2_HOST:-192.168.0.238}}"
SELF="${SELF_HOST:-192.168.0.210}"          # the address ha-2 uses to reach .210 back
KEY="${CLUSTER_KEY:-/home/visko/.ssh/id_cluster}"
SSHO="-i $KEY -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8"
CL(){ ssh $SSHO "visko@$HA2" "$@" 2>/dev/null; }

pass=0; fail=0; pend=0
ok(){ printf '  \033[32m✓\033[0m %s\n' "$1"; pass=$((pass+1)); }
no(){ printf '  \033[31m✗\033[0m %s\n' "$1"; fail=$((fail+1)); }
pd(){ printf '  \033[33m⧗\033[0m %s  (pending — %s)\n' "$1" "$2"; pend=$((pend+1)); }
grp(){ printf '\n\033[1m%s\033[0m\n' "$1"; }

printf '\033[1mPhase -1 readiness — target ha-2 @ %s (control plane .210 @ %s)\033[0m\n' "$HA2" "$SELF"

grp "TIER 1 — Access hooks (the control plane)"
CL 'true' && ok ".210 -> ha-2 SSH (id_cluster)" || no ".210 -> ha-2 SSH (id_cluster)"
[ "$(CL "ssh -i ~/.ssh/id_cluster -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=6 visko@$SELF hostname")" = "ha-dev" ] \
  && ok "ha-2 -> .210 SSH (id_cluster)" || no "ha-2 -> .210 SSH (id_cluster)"
[ "$(CL 'sudo -n true && echo ok')" = ok ] && ok "passwordless sudo on ha-2 (sudo -n)" || no "passwordless sudo on ha-2"
[ "$(CL 'touch ~/home_automation/instance/.pf && rm -f ~/home_automation/instance/.pf && echo ok')" = ok ] && ok "fs write access on ha-2" || no "fs write access on ha-2"

grp "TIER 1 — Base OS"
[ "$(CL '. /etc/os-release; echo $VERSION_ID')" = 13 ] && ok "Debian 13 (trixie)" || no "Debian 13"
fr=$(CL "df -BG / | awk 'NR==2{gsub(\"G\",\"\",\$4); print \$4}'"); [ "${fr:-0}" -ge 20 ] && ok "disk free ${fr}G (>=20G)" || no "disk free ${fr}G (<20G)"
pyv=$(CL 'python3 -V 2>&1'); case "$pyv" in *3.13.*) ok "python 3.13.x";; *) no "python 3.13.x (got: $pyv)";; esac

grp "TIER 1 — Packages (offline-critical — no internet after the gap)"
for p in git curl ca-certificates mosquitto mosquitto-clients bluez bluetooth \
         firmware-mediatek firmware-realtek keepalived chrony ntfy sqlite3 python3-venv rsync; do
  [ "$(CL "dpkg -s $p >/dev/null 2>&1 && echo ok")" = ok ] && ok "pkg $p" || no "pkg $p MISSING"
done

grp "TIER 1 — Python venv deps"
[ "$(CL 'cd ~/home_automation && venv/bin/python -c "import bleak,duckdb,pyarrow,fastapi,uvicorn,paho.mqtt,yaml,httpx,uvloop" >/dev/null 2>&1 && echo ok')" = ok ] \
  && ok "venv imports (bleak, duckdb, pyarrow, fastapi, uvicorn, paho, yaml, httpx, uvloop)" || no "venv imports FAIL"

grp "TIER 1 — Services (isolated app layer)"
for s in mosquitto bluetooth ha-writer ha-api ha-edge-mapper; do
  [ "$(CL "systemctl is-active $s")" = active ] && ok "$s active" || no "$s not active"
done
[ "$(CL 'systemctl is-active ha-controller')" != active ] && ok "ha-controller OFF (correct for non-dictator)" || no "ha-controller ON (must be OFF)"
[ "$(CL 'systemctl is-active keepalived')" != active ] && ok "keepalived inactive (configured in Phase 1)" || no "keepalived active (should be inactive)"

grp "TIER 1 — Offline artifacts + secrets prepositioned"
[ "$(CL 'ls ~/home_automation/instance/openwrt/*.img >/dev/null 2>&1 && echo ok')" = ok ] && ok "OpenWRT images present (router_reconcile)" || no "OpenWRT images MISSING"
[ "$(CL 'test -s ~/home_automation/instance/.master_pass && echo ok')" = ok ] && ok ".master_pass prepositioned (critical|preposition)" || no ".master_pass MISSING"

grp "TIER 1 — Repo parity"
h2=$(CL 'cd ~/home_automation && git rev-parse --short HEAD'); loc=$(git rev-parse --short HEAD)
[ "$h2" = "$loc" ] && ok "ha-2 repo @ $h2 (== .210)" || no "ha-2 repo @ $h2 != .210 @ $loc"

grp "TIER 2 — Deferred (proven in later phases; ride the SSH channel, no internet needed)"
[ "$(CL 'test -f ~/home_automation/server/maintenance/device_push.py && echo ok')" = ok ] \
  && ok "cross-network migration tooling deployed" || pd "cross-network migration tooling" "Phase 3 build"
[ "$(CL 'curl -fsS -o /dev/null -w %{http_code} http://localhost:8123/health 2>/dev/null')" = 200 ] \
  && ok "ha-2 API /health 200 (control-plane base OK)" || pd "ha-2 API /health" "start/verify in phase"
pd "full device :import dry-run over the .210 bridge" "needs Phase 2 bridge + Phase 3 tooling"

echo
printf '\033[1mSUMMARY:\033[0m %d passed · %d failed · %d pending\n' "$pass" "$fail" "$pend"
if [ "$fail" -eq 0 ]; then
  printf '\033[32m✓ TIER 1 GREEN — Phase -1 readiness gate PASSED.\033[0m  (%d Tier-2 item(s) pending later phases)\n' "$pend"; exit 0
else
  printf '\033[31m✗ TIER 1 has %d failure(s) — ha-2 is NOT ready to change networks.\033[0m\n' "$fail"; exit 1
fi
