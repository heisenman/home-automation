#!/usr/bin/env bash
# failover-drill — a REVERSIBLE, scripted failover exercise for the dictator<->standby pair (ROADMAP A3).
# It does NOT implement failover (keepalived/notify.sh/primary-watch already do); it ORCHESTRATES and
# OBSERVES one, then ASSERTS the invariants and FAILS BACK — capturing timings so we know our RTO.
#
# PAIRS (two independent failover domains):
#   --household  (default)  .210 <-> .245, VIP .0.200, unit ha-controller on both        (dev/household)
#   --airgap                ha-2 <-> .210, VIP .1.200, ha-controller(ha-2)/ha-ag-controller(.210)  (production)
#                           fence is over the cluster BUS (ADR-0033); the drill verifies the fence listener acted.
#
# COMMANDS:
#   (default)          --dry-run preflight: READ-ONLY. Verifies prereqs, prints the plan + rollback, no changes.
#   --run              LIVE drill: induces a real failover, asserts, fails back. Gated (see below).
#   checkpoint         capture the current baseline snapshot to instance/drill-checkpoints/ (no changes).
#   restore            RECOVERY verb: force keepalived up on BOTH boxes, wait for the primary to reclaim the
#                      VIP, assert a single controller, run cluster-doctor. Idempotent, safe to run anytime.
#
# SAFETY MODEL (read this):
#   * DEFAULT = --dry-run. Safe anytime, including on the live dictator.
#   * --run briefly removes control from the current dictator and makes the STANDBY the controller. On
#     --household that's .245 (Hugh's fileserver); on --airgap that's .210 seizing PRODUCTION ha-2's role.
#     Either way --run refuses unless HA_DRILL_CONFIRM=I-UNDERSTAND is set (Hugh-OK + a window).
#   * --actuate (with --run) also proves the new dictator can actuate. The MOST gated step. Off by default.
#   * THREE recovery nets on a live run: (1) an EXIT trap restarts keepalived on both boxes; (2) a dead-man
#     transient timer auto-runs `restore` after DRILL_DEADMAN_S even if this process is KILLED; (3) the
#     standalone `restore` verb any operator can run by hand. A half-run drill still leaves a WORKING cluster
#     (the standby serves) — restore just fails it back.
#
#   env: PRIMARY_HOST STANDBY_HOST VIP BROKER CLUSTER_KEY(id_cluster) DRILL_TIMEOUT(=45s/transition)
#        RTO_BUDGET_S(=600) DRILL_DEADMAN_S(=300) HA_DRILL_CONFIRM(for --run) DRILL_CKPT_DIR
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"
[ -f "$REPO/instance/cluster.env" ] && . "$REPO/instance/cluster.env"

# ---- args -------------------------------------------------------------------------------------------
MODE="dry-run"; ACTUATE=0; PAIR="household"; CMD=""
for a in "$@"; do case "$a" in
  --run) MODE="run";; --dry-run) MODE="dry-run";; --actuate) ACTUATE=1;;
  --airgap) PAIR="airgap";; --household) PAIR="household";;
  restore) CMD="restore";; checkpoint) CMD="checkpoint";;
  -h|--help) sed -n '2,35p' "$0"; exit 0;;
  *) echo "unknown arg: $a"; exit 2;; esac; done

# ---- pair config (per-domain defaults; env overrides win) -------------------------------------------
if [ "$PAIR" = airgap ]; then
  PRIMARY="${PRIMARY_HOST:-192.168.1.210}"          # ha-2 (air-gap dictator)
  STANDBY="${STANDBY_HOST:-192.168.1.245}"          # .210's air-gap leg
  VIP="${VIP_AIRGAP:-192.168.1.200}"
  PRIMARY_CU="${PRIMARY_CU:-ha-controller}"         # ha-2's controller unit
  STANDBY_CU="${STANDBY_CU:-ha-ag-controller}"      # .210's air-gap controller unit
  PRIMARY_LISTENER="${PRIMARY_LISTENER:-ha-fence-listener}"
  STANDBY_LISTENER="${STANDBY_LISTENER:-ha-ag-fence-listener}"
  HB_NODES="${HB_NODES:-210}"                       # only .210 heartbeats the air-gap bus; ha-2's liveness = VRRP
  FENCE_BUS=1
else
  PRIMARY="${PRIMARY_HOST:-192.168.0.210}"
  STANDBY="${STANDBY_HOST:-192.168.0.245}"
  VIP="${VIP:-192.168.0.200}"
  PRIMARY_CU="${PRIMARY_CU:-ha-controller}"
  STANDBY_CU="${STANDBY_CU:-ha-controller}"
  PRIMARY_LISTENER="${PRIMARY_LISTENER:-ha-fence-listener}"
  STANDBY_LISTENER="${STANDBY_LISTENER:-ha-fence-listener}"
  HB_NODES="${HB_NODES:-210 245}"
  FENCE_BUS=0
fi
BROKER="${BROKER:-$VIP}"
KEY="${CLUSTER_KEY:-$HOME/.ssh/id_cluster}"
TIMEOUT="${DRILL_TIMEOUT:-45}"
RTO_BUDGET_S="${RTO_BUDGET_S:-600}"                 # acceptable control-outage ceiling (Hugh 2026-06-25: 10 min)
DEADMAN_S="${DRILL_DEADMAN_S:-300}"
CKPT_DIR="${DRILL_CKPT_DIR:-$REPO/instance/drill-checkpoints}"

pass=0; fail=0; warn=0
ok(){ printf '  [PASS] %s\n' "$*"; pass=$((pass+1)); }
no(){ printf '  [FAIL] %s\n' "$*"; fail=$((fail+1)); }
wn(){ printf '  [WARN] %s\n' "$*"; warn=$((warn+1)); }
hdr(){ printf '\n== %s ==\n' "$*"; }
# NOTE: the air-gap boundary locks port 47222 to reconcile-only; the drill's mgmt SSH uses :22 (household admin
# line on ha-2, still a full shell) — do NOT point this at 47222.
SSH(){ ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=6 -o StrictHostKeyChecking=accept-new "$@"; }
SELF_IPS=" $(hostname -I 2>/dev/null) "
is_self(){ [[ "$SELF_IPS" == *" $1 "* ]]; }
# run a command on $1, local sudo if this box else cluster SSH. Honors dry-run (prints only) UNLESS forced.
run_on(){ local h="$1"; shift
  if [ "$MODE" = dry-run ] && [ "${FORCE_RUN:-0}" != 1 ]; then echo "    (dry-run) would run on $h: $*"; return 0; fi
  if is_self "$h"; then sudo "$@"; else SSH "visko@$h" "sudo $*"; fi; }
state_on(){ local h="$1" unit="$2"
  if is_self "$h"; then systemctl is-active "$unit" 2>/dev/null || true
  else SSH "visko@$h" "systemctl is-active $unit" 2>/dev/null || echo unreachable; fi; }
vip_on(){ local h="$1"
  if is_self "$h"; then ip -o addr show 2>/dev/null | grep -qw "$VIP"
  else SSH "visko@$h" "ip -o addr show 2>/dev/null | grep -qw $VIP"; fi; }
cu_of(){ case "$1" in "$PRIMARY") echo "$PRIMARY_CU";; "$STANDBY") echo "$STANDBY_CU";; *) echo "$PRIMARY_CU";; esac; }
listener_of(){ case "$1" in "$PRIMARY") echo "$PRIMARY_LISTENER";; *) echo "$STANDBY_LISTENER";; esac; }
# best-effort: did $1's fence listener log a VALID fence in the last ~90s? (proves the BUS fence acted)
bus_fence_seen(){ local h="$1" u; u="$(listener_of "$h")"
  if is_self "$h"; then journalctl -u "$u" --since '90 seconds ago' --no-pager -q 2>/dev/null | grep -q 'VALID fence'
  else SSH "visko@$h" "journalctl -u $u --since '90 seconds ago' --no-pager -q 2>/dev/null | grep -q 'VALID fence'"; fi; }
bus_hb(){ timeout 6 mosquitto_sub -h "$BROKER" -t "ha/cluster/$1/heartbeat" -C 1 -W 5 2>/dev/null; }
wait_until(){ local desc="$1" start now; start=$(date +%s)
  while :; do if eval "$2"; then now=$(date +%s); echo $((now-start)); return 0; fi
    now=$(date +%s); [ $((now-start)) -ge "$TIMEOUT" ] && { echo $((now-start)); return 1; }
    sleep 2; done; }

# ---- recovery: baseline capture + restore + dead-man -------------------------------------------------
capture_baseline(){ local who="${1:-?}"
  mkdir -p "$CKPT_DIR"; local f="$CKPT_DIR/baseline-$PAIR.txt"
  { echo "# failover-drill baseline  pair=$PAIR  at=$(date -Is)"
    echo "PRIMARY=$PRIMARY STANDBY=$STANDBY VIP=$VIP"
    echo "vip_holder=$who"
    echo "primary_controller($PRIMARY_CU)=$(state_on "$PRIMARY" "$PRIMARY_CU")"
    echo "standby_controller($STANDBY_CU)=$(state_on "$STANDBY" "$STANDBY_CU")"
    echo "primary_keepalived=$(state_on "$PRIMARY" keepalived)"
    echo "standby_keepalived=$(state_on "$STANDBY" keepalived)"
  } > "$f"; echo "  baseline captured -> $f"; }

do_restore(){ FORCE_RUN=1
  hdr "RESTORE baseline ($PAIR) — ensure keepalived on both; wait for $PRIMARY to reclaim the VIP"
  for h in "$PRIMARY" "$STANDBY"; do run_on "$h" systemctl start keepalived >/dev/null 2>&1 && echo "    keepalived ensured on $h" || echo "    WARN: could not ensure keepalived on $h"; done
  local t; t=$(wait_until "vip->primary" 'vip_on "$PRIMARY"') && ok "VIP reclaimed by $PRIMARY in ${t}s" || no "$PRIMARY did NOT reclaim VIP within ${TIMEOUT}s (investigate)"
  [ "$(state_on "$STANDBY" "$STANDBY_CU")" != active ] && ok "standby $STANDBY controller stopped" || wn "standby $STANDBY controller still active — primary-watch should demote it"
  [ "$(state_on "$PRIMARY" "$PRIMARY_CU")" = active ] && ok "primary $PRIMARY controller active" || wn "primary $PRIMARY controller not active yet"
  "$HERE/cluster-doctor.sh" >/dev/null 2>&1 && ok "cluster-doctor HEALTHY" || wn "cluster-doctor not green — investigate"; }

arm_deadman(){ sudo systemd-run --unit="drill-deadman-$PAIR" --on-active="$DEADMAN_S" \
    /usr/bin/runuser -u "$(id -un)" -- /bin/bash "$0" restore "--$PAIR" >/dev/null 2>&1 \
    && echo "  dead-man armed: auto-restore in ${DEADMAN_S}s if this process dies (cancel: systemctl stop drill-deadman-$PAIR.timer)" \
    || wn "could not arm dead-man (trap + manual restore still cover you)"; }
disarm_deadman(){ sudo systemctl stop "drill-deadman-$PAIR.timer" 2>/dev/null; sudo systemctl reset-failed "drill-deadman-$PAIR.service" 2>/dev/null || true; }

# ---- standalone commands ----------------------------------------------------------------------------
echo "failover-drill $(date -Is)  pair=$PAIR mode=${CMD:-$MODE} actuate=$ACTUATE  primary=$PRIMARY standby=$STANDBY vip=$VIP"
CUR_MASTER=""; for h in "$PRIMARY" "$STANDBY"; do vip_on "$h" 2>/dev/null && CUR_MASTER="$h"; done

if [ "$CMD" = restore ]; then do_restore; exit 0; fi
if [ "$CMD" = checkpoint ]; then hdr "Checkpoint baseline"; capture_baseline "$CUR_MASTER"; exit 0; fi

# ---- preflight (ALWAYS; this is the whole of dry-run) -----------------------------------------------
hdr "Preflight ($PAIR)"
if [ -x "$HERE/cluster-doctor.sh" ]; then
  if "$HERE/cluster-doctor.sh" >/tmp/drill-doctor.$$ 2>&1; then ok "cluster-doctor: HEALTHY (preconditions green)"
  else wn "cluster-doctor reports issues (see below) — review before a live run"; sed 's/^/      /' /tmp/drill-doctor.$$ | tail -25; fi
  rm -f /tmp/drill-doctor.$$
else wn "cluster-doctor.sh not found/executable — skipping the invariant precheck"; fi

if [ -n "$CUR_MASTER" ]; then ok "current dictator (VIP holder) = $CUR_MASTER"
else no "could not determine VIP holder (SSH to peer? run from a box with cluster keys)"; fi
TARGET=""; [ "$CUR_MASTER" = "$PRIMARY" ] && TARGET="$STANDBY"; [ "$CUR_MASTER" = "$STANDBY" ] && TARGET="$PRIMARY"

# the takeover target must hold the full critical file set or it can't actuate after seizing
if [ -n "$TARGET" ]; then
  MANIFEST="$HERE/dictator-files.manifest"
  if [ -f "$MANIFEST" ]; then
    miss=""; while read -r f; do
      if is_self "$TARGET"; then [ -s "$REPO/$f" ] || miss="$miss $f"
      else SSH "visko@$TARGET" "test -s ${REPO_REMOTE:-/home/visko/home_automation}/$f" || miss="$miss $f"; fi
    done < <(awk -F'|' '/^[[:space:]]*#/||NF<3{next}{gsub(/^[ \t]+|[ \t]+$/,"",$1);gsub(/^[ \t]+|[ \t]+$/,"",$2);if($2=="critical")print $1}' "$MANIFEST")
    [ -z "$miss" ] && ok "takeover target $TARGET has all critical dictator files" \
                    || no "takeover target $TARGET MISSING:$miss — it would seize control but NOT actuate"
  fi
fi
for n in $HB_NODES; do m=$(bus_hb "$n"); [ -n "$m" ] && ok "node $n heartbeat present on bus" || wn "node $n no retained heartbeat"; done
[ "$FENCE_BUS" = 1 ] && { for h in "$PRIMARY" "$STANDBY"; do [ "$(state_on "$h" "$(listener_of "$h")")" = active ] && ok "fence listener up on $h ($(listener_of "$h"))" || wn "fence listener NOT active on $h — bus fence won't act"; done; }

hdr "Drill plan (what --run would do)"
cat <<PLAN
  1. baseline   : capture VIP holder ($CUR_MASTER) + controller/keepalived states -> $CKPT_DIR/baseline-$PAIR.txt
  2. arm        : dead-man transient timer (auto-restore in ${DEADMAN_S}s if this process is killed)
  3. induce     : stop keepalived on $CUR_MASTER -> VRRP fails over; $TARGET promotes (notify MASTER: fences
                  $CUR_MASTER via $([ "$FENCE_BUS" = 1 ] && echo "the cluster BUS" || echo "ssh"), starts its own controller, remounts ha-api)
  4. observe    : wait (<=${TIMEOUT}s) for VIP+controller on $TARGET; assert single-dictator$([ "$FENCE_BUS" = 1 ] && echo " + a VALID fence in $CUR_MASTER's listener log")
  5. actuate    : $([ "$ACTUATE" = 1 ] && echo "prove $TARGET can actuate" || echo "(skipped; --actuate to include — needs Hugh OK)")
  6. failback   : start keepalived on $CUR_MASTER -> it preempts, reclaims VIP+controller; $TARGET auto-demotes
  7. verify     : cluster-doctor HEALTHY; report transition timings (= measured RTO); disarm dead-man
  RECOVERY: EXIT trap restores keepalived on both; dead-man auto-restores if killed; \`$0 restore --$PAIR\` by hand.
PLAN

if [ -z "$CMD" ] && [ "$MODE" = dry-run ]; then
  hdr "Verdict (DRY RUN — no changes made)"
  printf '  %d pass, %d warn, %d FAIL\n' "$pass" "$warn" "$fail"
  if [ "$fail" -eq 0 ]; then echo "  => PREFLIGHT GREEN — ready for a gated live run (HA_DRILL_CONFIRM=I-UNDERSTAND $0 --run --$PAIR)"; exit 0
  else echo "  => PREFLIGHT NOT READY — resolve FAILs before any live run"; exit 1; fi
fi

# ---- live drill (gated) -----------------------------------------------------------------------------
if [ "${HA_DRILL_CONFIRM:-}" != "I-UNDERSTAND" ]; then
  echo; echo "REFUSED: --run is a LIVE failover that briefly removes control from $CUR_MASTER and makes"
  echo "         $TARGET the controller. Re-run with: HA_DRILL_CONFIRM=I-UNDERSTAND $0 --run --$PAIR   (Hugh-OK + window)"; exit 3; fi
[ "$fail" -eq 0 ] || { echo "REFUSED: preflight has FAILs — fix them first."; exit 1; }
[ -n "$CUR_MASTER" ] && [ -n "$TARGET" ] || { echo "REFUSED: could not resolve master/target."; exit 1; }

restore_trap(){ echo; hdr "ROLLBACK (trap): ensure keepalived running on both boxes"; FORCE_RUN=1
  for h in "$PRIMARY" "$STANDBY"; do run_on "$h" systemctl start keepalived >/dev/null 2>&1 && echo "    keepalived ensured on $h" || echo "    WARN: could not ensure keepalived on $h"; done; }
trap restore_trap EXIT

hdr "1. Baseline"; capture_baseline "$CUR_MASTER"; ok "baseline dictator = $CUR_MASTER; target = $TARGET"
hdr "2. Arm dead-man"; arm_deadman
hdr "3. Induce failover (stop keepalived on $CUR_MASTER)"; run_on "$CUR_MASTER" systemctl stop keepalived
hdr "4. Observe takeover on $TARGET (timeout ${TIMEOUT}s)"
t_vip=$(wait_until "vip->target" 'vip_on "$TARGET"') && ok "VIP moved to $TARGET in ${t_vip}s" || no "VIP did NOT reach $TARGET within ${TIMEOUT}s"
t_ctl=$(wait_until "ctl->target" '[ "$(state_on "$TARGET" "$(cu_of "$TARGET")")" = active ]') && ok "controller ($(cu_of "$TARGET")) active on $TARGET in ${t_ctl}s" || no "controller did NOT start on $TARGET"
[ "$(state_on "$CUR_MASTER" "$(cu_of "$CUR_MASTER")")" = active ] && no "SPLIT-BRAIN: old master $CUR_MASTER still running its controller" || ok "old master $CUR_MASTER controller stopped (fenced)"
if [ "$FENCE_BUS" = 1 ]; then bus_fence_seen "$CUR_MASTER" && ok "BUS fence observed in $CUR_MASTER's listener log (fence-over-bus worked)" || wn "no VALID fence in $CUR_MASTER's listener log (it may have self-demoted via keepalived BACKUP before the fence)"; fi
if [ "$ACTUATE" = 1 ]; then hdr "5. Actuate from $TARGET (gated)"; wn "actuation proof not auto-wired — run tools/device_smoke_test.py against $TARGET's ha-api manually this run"; fi
hdr "6. Fail back (start keepalived on $CUR_MASTER; it preempts)"; run_on "$CUR_MASTER" systemctl start keepalived
t_back=$(wait_until "vip->master" 'vip_on "$CUR_MASTER"') && ok "VIP reclaimed by $CUR_MASTER in ${t_back}s" || no "$CUR_MASTER did NOT reclaim VIP"
t_demote=$(wait_until "ctl<-target" '[ "$(state_on "$TARGET" "$(cu_of "$TARGET")")" != active ]') && ok "$TARGET auto-demoted in ${t_demote}s" || no "$TARGET did NOT auto-demote"
hdr "7. Verify + timings"; "$HERE/cluster-doctor.sh" >/dev/null 2>&1 && ok "cluster-doctor HEALTHY post-drill" || wn "cluster-doctor not green post-drill — investigate"
rto=$(( ${t_vip:-0} + ${t_ctl:-0} ))
[ "$rto" -le "$RTO_BUDGET_S" ] && ok "failover RTO ~${rto}s within budget ${RTO_BUDGET_S}s" || no "failover RTO ~${rto}s EXCEEDS budget ${RTO_BUDGET_S}s (tighten VRRP/heartbeat timing)"
echo "  RTO (failover): VIP ${t_vip:-?}s + controller ${t_ctl:-?}s = ~${rto}s (budget ${RTO_BUDGET_S}s) ; failback: VIP ${t_back:-?}s / demote ${t_demote:-?}s"
hdr "Verdict"; printf '  %d pass, %d warn, %d FAIL\n' "$pass" "$warn" "$fail"
[ "$fail" -eq 0 ] && echo "  => DRILL PASSED" || echo "  => DRILL HAD FAILURES (see above)"
disarm_deadman
trap - EXIT; restore_trap
[ "$fail" -eq 0 ]
