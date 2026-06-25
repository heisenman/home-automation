#!/usr/bin/env bash
# failover-drill — a REVERSIBLE, scripted failover exercise for the dictator<->standby pair (ROADMAP A3).
# It does NOT implement failover (keepalived/notify.sh/primary-watch already do); it ORCHESTRATES and
# OBSERVES one, then ASSERTS the invariants and FAILS BACK — capturing timings so we know our RTO.
#
# SAFETY MODEL (read this):
#   * DEFAULT = --dry-run: READ-ONLY preflight. Verifies prerequisites, prints the exact drill plan +
#     rollback plan, makes NO changes. Safe to run anytime, including on the live dictator.
#   * --run = LIVE drill: induces a real failover. This briefly removes control from the current dictator
#     and makes the STANDBY the controller. On the 210<->245 pair that means .245 (Hugh's fileserver)
#     transiently becomes the controller -> requires Hugh's explicit OK + a window, so --run refuses
#     unless HA_DRILL_CONFIRM=I-UNDERSTAND is also set.
#   * --actuate (with --run) = also prove the new dictator can actuate the Midea. The MOST gated step
#     (actuating from .245). Off by default.
#   * A trap guarantees keepalived is restarted on BOTH boxes on ANY exit, so an aborted drill cannot
#     leave the cluster headless.
#
#   env: PRIMARY_HOST(=210) STANDBY_HOST(=245) VIP(=.200) BROKER(=VIP) CLUSTER_KEY(id_cluster)
#        CONTROLLER_UNIT(=ha-controller) DRILL_TIMEOUT(=45s per transition) HA_DRILL_CONFIRM(for --run)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"
[ -f "$REPO/instance/cluster.env" ] && . "$REPO/instance/cluster.env"
PRIMARY="${PRIMARY_HOST:-192.168.0.210}"
STANDBY="${STANDBY_HOST:-192.168.0.245}"
VIP="${VIP:-192.168.0.200}"
BROKER="${BROKER:-$VIP}"
CONTROLLER_UNIT="${CONTROLLER_UNIT:-ha-controller}"
KEY="${CLUSTER_KEY:-$HOME/.ssh/id_cluster}"
TIMEOUT="${DRILL_TIMEOUT:-45}"
RTO_BUDGET_S="${RTO_BUDGET_S:-600}"   # acceptable control-outage ceiling (Hugh 2026-06-25: 10 min for the
                                      # current thermal load). Future: derive from the strictest actuator's
                                      # max_control_outage_s (a per-device trait set in the PWA). The drill
                                      # PASS/FAILs the MEASURED failover time against this.
REPO_REMOTE="${REPO_REMOTE:-/home/visko/home_automation}"
MODE="dry-run"; ACTUATE=0
for a in "$@"; do case "$a" in
  --run) MODE="run";; --dry-run) MODE="dry-run";; --actuate) ACTUATE=1;;
  -h|--help) sed -n '2,30p' "$0"; exit 0;;
  *) echo "unknown arg: $a"; exit 2;; esac; done

pass=0; fail=0; warn=0
ok(){ printf '  [PASS] %s\n' "$*"; pass=$((pass+1)); }
no(){ printf '  [FAIL] %s\n' "$*"; fail=$((fail+1)); }
wn(){ printf '  [WARN] %s\n' "$*"; warn=$((warn+1)); }
hdr(){ printf '\n== %s ==\n' "$*"; }
SSH(){ ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=6 -o StrictHostKeyChecking=accept-new "$@"; }
SELF_IPS=" $(hostname -I 2>/dev/null) "
is_self(){ [[ "$SELF_IPS" == *" $1 "* ]]; }
# run a command on $1, using local sudo if it's THIS box, else cluster SSH. Honors dry-run (prints only).
run_on(){ local h="$1"; shift
  if [ "$MODE" = dry-run ]; then echo "    (dry-run) would run on $h: $*"; return 0; fi
  if is_self "$h"; then sudo "$@"; else SSH "visko@$h" "sudo $*"; fi; }
state_on(){ local h="$1" unit="$2"   # active|inactive|unreachable
  if is_self "$h"; then systemctl is-active "$unit" 2>/dev/null || true
  else SSH "visko@$h" "systemctl is-active $unit" 2>/dev/null || echo unreachable; fi; }
vip_on(){ local h="$1"
  if is_self "$h"; then ip -o addr show 2>/dev/null | grep -qw "$VIP"
  else SSH "visko@$h" "ip -o addr show 2>/dev/null | grep -qw $VIP"; fi; }
bus_hb(){ timeout 6 mosquitto_sub -h "$BROKER" -t "ha/cluster/$1/heartbeat" -C 1 -W 5 2>/dev/null; }
# poll until predicate (bash expr) true or TIMEOUT; echoes elapsed seconds, returns 0/1
wait_until(){ local desc="$1" start now; start=$(date +%s)
  while :; do if eval "$2"; then now=$(date +%s); echo $((now-start)); return 0; fi
    now=$(date +%s); [ $((now-start)) -ge "$TIMEOUT" ] && { echo $((now-start)); return 1; }
    sleep 2; done; }

echo "failover-drill $(date -Is)  mode=$MODE actuate=$ACTUATE  primary=$PRIMARY standby=$STANDBY vip=$VIP"

# ---- preflight (ALWAYS; this is the whole of dry-run) ----------------------------------------------
hdr "Preflight"
command -v failover/cluster-doctor.sh >/dev/null 2>&1 || true
if [ -x "$HERE/cluster-doctor.sh" ]; then
  if "$HERE/cluster-doctor.sh" >/tmp/drill-doctor.$$ 2>&1; then ok "cluster-doctor: HEALTHY (preconditions green)"
  else wn "cluster-doctor reports issues (see below) — review before a live run"; sed 's/^/      /' /tmp/drill-doctor.$$ | tail -25; fi
  rm -f /tmp/drill-doctor.$$
else wn "cluster-doctor.sh not found/executable — skipping the invariant precheck"; fi

# who is the current dictator?
CUR_MASTER=""; for h in "$PRIMARY" "$STANDBY"; do vip_on "$h" 2>/dev/null && CUR_MASTER="$h"; done
if [ -n "$CUR_MASTER" ]; then ok "current dictator (VIP holder) = $CUR_MASTER"
else no "could not determine VIP holder (SSH to peer? run from a box with cluster keys)"; fi
TARGET=""; [ "$CUR_MASTER" = "$PRIMARY" ] && TARGET="$STANDBY"; [ "$CUR_MASTER" = "$STANDBY" ] && TARGET="$PRIMARY"

# the standby (takeover target) must hold the full critical file set or it can't actuate after seizing
if [ -n "$TARGET" ]; then
  MANIFEST="$HERE/dictator-files.manifest"
  if [ -f "$MANIFEST" ]; then
    miss=""; while read -r f; do
      if is_self "$TARGET"; then [ -s "$REPO/$f" ] || miss="$miss $f"
      else SSH "visko@$TARGET" "test -s $REPO_REMOTE/$f" || miss="$miss $f"; fi
    done < <(awk -F'|' '/^[[:space:]]*#/||NF<3{next}{gsub(/^[ \t]+|[ \t]+$/,"",$1);gsub(/^[ \t]+|[ \t]+$/,"",$2);if($2=="critical")print $1}' "$MANIFEST")
    [ -z "$miss" ] && ok "takeover target $TARGET has all critical dictator files" \
                    || no "takeover target $TARGET MISSING:$miss — it would seize control but NOT actuate"
  fi
fi
# heartbeats fresh on the bus
for n in 210 245; do m=$(bus_hb "$n"); [ -n "$m" ] && ok "node $n heartbeat present on bus" || wn "node $n no retained heartbeat"; done

hdr "Drill plan (what --run would do)"
cat <<PLAN
  1. baseline   : record VIP holder ($CUR_MASTER), controller node, cluster-doctor snapshot
  2. induce     : stop keepalived on $CUR_MASTER  -> VRRP fails over; $TARGET promotes (notify MASTER:
                  fences $CUR_MASTER's controller, starts its own, remounts ha-api on the VIP)
  3. observe    : wait (<=${TIMEOUT}s) for VIP+controller to land on $TARGET; assert single-dictator invariant
  4. actuate    : ${ACTUATE:+ISSUE a gated Midea command from $TARGET and confirm ack}${ACTUATE:+ }$([ "$ACTUATE" = 0 ] && echo "(skipped; pass --actuate to include — needs Hugh OK)")
  5. failback   : start keepalived on $CUR_MASTER -> it preempts, reclaims VIP+controller; primary-watch
                  auto-demotes $TARGET; assert back to baseline
  6. verify     : cluster-doctor HEALTHY again; report transition timings (= measured RTO)
  ROLLBACK/SAFETY: a trap restarts keepalived on BOTH boxes on any exit, so an abort can't leave the
                   cluster headless. Reversible end-to-end.
PLAN

if [ "$MODE" = dry-run ]; then
  hdr "Verdict (DRY RUN — no changes made)"
  printf '  %d pass, %d warn, %d FAIL\n' "$pass" "$warn" "$fail"
  if [ "$fail" -eq 0 ]; then echo "  => PREFLIGHT GREEN — ready for a gated live run (HA_DRILL_CONFIRM=I-UNDERSTAND ./failover-drill.sh --run)"; exit 0
  else echo "  => PREFLIGHT NOT READY — resolve FAILs before any live run"; exit 1; fi
fi

# ---- live drill (gated) ----------------------------------------------------------------------------
if [ "${HA_DRILL_CONFIRM:-}" != "I-UNDERSTAND" ]; then
  echo; echo "REFUSED: --run is a LIVE failover that briefly removes control from $CUR_MASTER and makes"
  echo "         $TARGET the controller. Re-run with: HA_DRILL_CONFIRM=I-UNDERSTAND $0 --run   (Hugh-OK + window)"; exit 3; fi
[ "$fail" -eq 0 ] || { echo "REFUSED: preflight has FAILs — fix them first."; exit 1; }
[ -n "$CUR_MASTER" ] && [ -n "$TARGET" ] || { echo "REFUSED: could not resolve master/target."; exit 1; }

restore(){ echo; hdr "ROLLBACK (trap): ensure keepalived running on both boxes"
  for h in "$PRIMARY" "$STANDBY"; do run_on "$h" systemctl start keepalived 2>/dev/null && echo "    keepalived ensured on $h" || echo "    WARN: could not ensure keepalived on $h"; done; }
trap restore EXIT

hdr "1. Baseline"; ok "baseline dictator = $CUR_MASTER; target = $TARGET"
hdr "2. Induce failover (stop keepalived on $CUR_MASTER)"; run_on "$CUR_MASTER" systemctl stop keepalived
hdr "3. Observe takeover on $TARGET (timeout ${TIMEOUT}s)"
t_vip=$(wait_until "vip->target" 'vip_on "$TARGET"') && ok "VIP moved to $TARGET in ${t_vip}s" || no "VIP did NOT reach $TARGET within ${TIMEOUT}s"
t_ctl=$(wait_until "ctl->target" '[ "$(state_on "$TARGET" "$CONTROLLER_UNIT")" = active ]') && ok "controller active on $TARGET in ${t_ctl}s" || no "controller did NOT start on $TARGET"
[ "$(state_on "$CUR_MASTER" "$CONTROLLER_UNIT")" = active ] && no "SPLIT-BRAIN: old master $CUR_MASTER still running controller" || ok "old master $CUR_MASTER controller stopped (fenced)"
if [ "$ACTUATE" = 1 ]; then hdr "4. Actuate from $TARGET (gated)"; wn "actuation proof not yet wired — use tools/device_smoke_test.py against $TARGET's ha-api manually this run"; fi
hdr "5. Fail back (start keepalived on $CUR_MASTER; it preempts)"; run_on "$CUR_MASTER" systemctl start keepalived
t_back=$(wait_until "vip->master" 'vip_on "$CUR_MASTER"') && ok "VIP reclaimed by $CUR_MASTER in ${t_back}s" || no "$CUR_MASTER did NOT reclaim VIP"
t_demote=$(wait_until "ctl<-target" '[ "$(state_on "$TARGET" "$CONTROLLER_UNIT")" != active ]') && ok "$TARGET auto-demoted in ${t_demote}s" || no "$TARGET did NOT auto-demote"
hdr "6. Verify + timings"; "$HERE/cluster-doctor.sh" >/dev/null 2>&1 && ok "cluster-doctor HEALTHY post-drill" || wn "cluster-doctor not green post-drill — investigate"
rto=$(( ${t_vip:-0} + ${t_ctl:-0} ))   # induce->controller-up (sequential waits) = the control outage
[ "$rto" -le "$RTO_BUDGET_S" ] && ok "failover RTO ~${rto}s within budget ${RTO_BUDGET_S}s" || no "failover RTO ~${rto}s EXCEEDS budget ${RTO_BUDGET_S}s (tighten VRRP/heartbeat timing)"
echo "  RTO (failover): VIP ${t_vip:-?}s + controller ${t_ctl:-?}s = ~${rto}s (budget ${RTO_BUDGET_S}s) ; failback: VIP ${t_back:-?}s / demote ${t_demote:-?}s"
hdr "Verdict"; printf '  %d pass, %d warn, %d FAIL\n' "$pass" "$warn" "$fail"
[ "$fail" -eq 0 ] && echo "  => DRILL PASSED" || echo "  => DRILL HAD FAILURES (see above)"
trap - EXIT; restore
[ "$fail" -eq 0 ]
