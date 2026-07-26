#!/bin/bash
# reconcile-agent — ForceCommand wrapper for the dedicated failover SSH port (47222), ADR-0033 part2 item2.
#
# The air-gap boundary keeps ONE inbound SSH port open (47222, from the peer only). Without this, that port
# is a full shell + sudo via the shared id_cluster key — so a single compromised air-gap box could seize the
# other over the "closed" boundary. This wrapper restricts port-47222 SSH to ONLY the reconcile/sync verbs
# and their scp transfers; everything else is refused. Wired via `Match LocalPort 47222 / ForceCommand`.
#
# $SSH_ORIGINAL_COMMAND is whatever the client asked to run. We match it against an EXACT-shape allowlist:
# the fixed command templates the reconcile scripts emit, with the data fields charset-constrained to exclude
# shell metacharacters, and any '..' hard-refused (no path traversal / exfil via scp -f).
#
# scp note: clients MUST use legacy protocol (`scp -O`) so the server command is `scp -t|-f <path>` (OpenSSH 10
# defaults to the SFTP subsystem, which ForceCommand would break). The reconcile SCP() wrappers pass -O.
#
# Test:  RECONCILE_AGENT_DRYRUN=1 SSH_ORIGINAL_COMMAND='<cmd>' failover/cluster-ssh/reconcile-agent.sh
#        -> prints ALLOW/REFUSED instead of executing.
set -u
REPO=/home/visko/home_automation
c="${SSH_ORIGINAL_COMMAND:-}"

refuse(){ logger -t reconcile-agent -- "REFUSED: $c" 2>/dev/null || true; echo "reconcile-agent: refused (port 47222 is reconcile-only)" >&2; exit 1; }
run(){ if [ -n "${RECONCILE_AGENT_DRYRUN:-}" ]; then echo "ALLOW: $c"; exit 0; else exec bash -c "$c"; fi; }

# hard traversal guard — before any allow can match
case "$c" in *..*) refuse;; esac
[ -n "$c" ] || refuse

# charset classes (deliberately exclude shell metacharacters ; & | $ ( ) < > ` space etc.)
TS='[0-9A-Za-z:._-]+'      # cutoff timestamp, e.g. 2026-07-09T00:00:00Z
REL='[0-9A-Za-z._/-]+'     # relative repo path (parquet/hot.db/instance files)
NAME='[0-9A-Za-z._-]+'     # snap / device name
PID='[0-9]+'
HAUNIT='ha-[0-9A-Za-z._-]+\.(service|timer)'   # peer-repair: an ha-* unit this box may be asked to rerun
# Reconcile targets one of two KNOWN repo roots: this box's own repo, OR the co-resident airgap-standby
# checkout (~/ha-airgap-standby) — the ha-2 dictator reconciles its hot.db INTO the standby's separate repo,
# so its `cd <path>` uses that root. Bounded alternation of two fixed absolute paths; same charset/`..` guards.
ROOT='/home/visko/(home_automation|ha-airgap-standby)'

allow(){ [[ "$c" =~ ^$1$ ]] && run; }

# --- reconcile-history: export (pull) + merge (push) ---
allow "cd $ROOT && bash failover/reconcile-history\.sh --export /tmp/ha-recon-peer\.snap '$TS'"
allow "cd $ROOT && bash failover/reconcile-history\.sh --merge /tmp/ha-recon-local\.snap"
# --- reconcile-parquet: list + merge(+cleanup) + manifest rebuild ---
allow "cd $ROOT && bash failover/reconcile-parquet\.sh --list"
allow "cd $ROOT && bash failover/reconcile-parquet\.sh --merge '$REL' /tmp/pq-local-$PID\.parquet; rm -f /tmp/pq-local-$PID\.parquet"
allow "cd '$ROOT' && \[ -f 'tools/rebuild_parquet_manifest\.py' \] && venv/bin/python3 'tools/rebuild_parquet_manifest\.py' --parquet-dir '$REL' >/dev/null 2>&1"
# --- sync-standby: existence check + consistent sqlite snapshot + cleanup ---
allow "test -f $ROOT/$REL"
allow "command -v sqlite3 >/dev/null && sqlite3 $ROOT/$REL \"\.backup /tmp/$NAME\.snap\""
allow "rm -f /tmp/$NAME\.snap"
# --- scp transfers (legacy protocol; -t=write only to /tmp, -f=read from /tmp or repo) ---
allow "scp( -[A-Za-z])* -t /tmp/$NAME\.(snap|parquet)"
allow "scp( -[A-Za-z])* -f /tmp/$NAME\.(snap|parquet)"
allow "scp( -[A-Za-z])* -f $ROOT/$REL"
# --- peer-repair (plan crystalline-spinning-spindle): a peer may ask this box to RERUN one of its own ha-*
#     units. Bounded to ha-* (the NOPASSWD sudo scope), charset-clean, nothing else. File-pull needs no verb
#     here — it rides the `scp -f $ROOT/$REL` allow above (canonical repo copy, `..`-guarded). ---
allow "sudo -n systemctl restart $HAUNIT"

refuse
