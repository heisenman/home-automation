#!/usr/bin/env bash
# dehum_drift_report.sh — run the dehumidifier drift monitor (tools/dehum_drift.py) ON ha-2, piped and read-only,
# with tools/dehum_readiness.py bundled ahead of it as a library and the per-run history passed in. 1-min rungs
# only live 7 days, so the history is kept here on .210: instance/dehum-drift/runs.jsonl (one run per line, merged
# by unit+start; re-measured runs replace older values). Also files reports/<date>.md + latest.md, status.json and
# status-history.jsonl. Fired daily by visko's crontab (study doc §7, drift alert).
set -euo pipefail
HA2="${HA2:-192.168.1.200}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE="$ROOT/instance/dehum-drift"
OUT="$BASE/reports"
RUNS="$BASE/runs.jsonl"
mkdir -p "$OUT"
touch "$RUNS"
f="$OUT/$(TZ=America/Los_Angeles date +%F).md"
hist_b64="$(python3 -c 'import base64,json,sys; print(base64.b64encode(json.dumps([json.loads(l) for l in open(sys.argv[1]) if l.strip()]).encode()).decode())' "$RUNS")"
if { echo "_AS_LIBRARY = True"; echo "_HISTORY_B64 = \"$hist_b64\""
     cat "$ROOT/tools/dehum_readiness.py" "$ROOT/tools/dehum_drift.py"; } |
   ssh -o BatchMode=yes -o ConnectTimeout=30 "$HA2" 'cd home_automation && nice -n 15 python3 -' \
     >"$f.tmp" 2>"$OUT/last-error.log"; then
  sed -n '2s/^<!-- runs: \(.*\) -->$/\1/p' "$f.tmp" >"$BASE/new-runs.json"
  python3 - "$RUNS" "$BASE/new-runs.json" <<'EOF'
import json, os, sys
runs, new = sys.argv[1], sys.argv[2]
m = {}
for line in open(runs):
    if line.strip():
        o = json.loads(line)
        m[(o["unit"], o["start"])] = o
for o in (json.load(open(new)) if os.path.getsize(new) else []):
    m[(o["unit"], o["start"])] = o
with open(runs + ".tmp", "w") as fh:
    for k in sorted(m, key=lambda k: (k[1], k[0])):
        fh.write(json.dumps(m[k]) + "\n")
os.replace(runs + ".tmp", runs)
EOF
  sed -i '2d' "$f.tmp"                                   # the runs line is data, not report
  mv "$f.tmp" "$f"
  ln -sfn "$(basename "$f")" "$OUT/latest.md"
  sed -n '1s/^<!-- status: \(.*\) -->$/\1/p' "$f" >"$OUT/status.json"
  python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); print(json.dumps({"generated": s["generated"], "units": {u: v["state"] for u, v in s["units"].items()}}))' \
    "$OUT/status.json" >>"$OUT/status-history.jsonl"
  echo "report: $f ($(python3 -c 'import json,sys; print(", ".join(f"{u} {v["state"]}" for u, v in json.load(open(sys.argv[1]))["units"].items()))' "$OUT/status.json"); $(wc -l <"$RUNS") runs kept)"
else
  rc=$?
  rm -f "$f.tmp"
  echo "$(date -u -Is) FAILED rc=$rc: $(tail -1 "$OUT/last-error.log")" | tee -a "$OUT/failures.log" >&2
  exit "$rc"
fi
