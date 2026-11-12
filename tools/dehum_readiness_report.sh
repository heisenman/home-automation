#!/usr/bin/env bash
# dehum_readiness_report.sh — run the dehumidifier-study readiness report ON ha-2 (piped; read-only) and file it on
# .210 under instance/dehum-study/reports/: <date>.md + latest.md, status.json (machine-readable gate status) and
# status-history.jsonl (one line per run). Fired daily by visko's crontab on .210 (see the study doc §6c);
# same shape as aq_experiment_report.sh. Code-driven so the study stays on schedule without a session.
set -euo pipefail
HA2="${HA2:-192.168.1.200}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/instance/dehum-study/reports"
mkdir -p "$OUT"
f="$OUT/$(TZ=America/Los_Angeles date +%F).md"
# hysteresis: hand the last filed verdicts of the SE criteria to the report (dehum_readiness.py PREV_OK)
prev="$(python3 -c 'import json,sys
try:
    s = json.load(open(sys.argv[1]))
except Exception:
    print("{}"); sys.exit()
c = [x for g in s["gates"] for x in g["criteria"]]
f = lambda p: any(x["ok"] for x in c if x["name"].startswith(p))
print(repr({"hvac_zero": f("HVAC pair zero model"), "midea_zero": f("Midea pair zero offset")}))' "$OUT/status.json" 2>/dev/null || echo '{}')"
# completed gates stay complete: the date each gate was first filed complete (dehum_readiness.py PREV_DONE)
done="$(python3 -c 'import json,sys
d = {}
try:
    for line in open(sys.argv[1]):
        h = json.loads(line)
        for gid, v in h["gates"].items():
            if v[2] and gid not in d:
                d[gid] = h["generated"][:10]
except Exception:
    pass
print(repr(d))' "$OUT/status-history.jsonl" 2>/dev/null || echo '{}')"
# rotation blocks already scored (P3): ha-2 keeps minute data 7 days, so older scores live here (blocks.json)
blocks="$(python3 -c 'import json,sys
try:
    print(repr(json.load(open(sys.argv[1]))))
except Exception:
    print("{}")' "$OUT/blocks.json" 2>/dev/null || echo '{}')"
if { echo "_PREV_OK = $prev"; echo "_PREV_DONE = $done"; echo "_PREV_BLOCKS = $blocks"; cat "$ROOT/tools/dehum_readiness.py"; } |
   ssh -o BatchMode=yes -o ConnectTimeout=30 "$HA2" 'cd home_automation && nice -n 15 python3 -' \
     >"$f.tmp" 2>"$OUT/last-error.log"; then
  mv "$f.tmp" "$f"
  ln -sfn "$(basename "$f")" "$OUT/latest.md"
  sed -n '1s/^<!-- status: \(.*\) -->$/\1/p' "$f" >"$OUT/status.json"
  python3 -c 'import json,sys; b=json.load(open(sys.argv[1])).get("blocks"); b is not None and json.dump(b, open(sys.argv[2], "w"))' \
    "$OUT/status.json" "$OUT/blocks.json"
  python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); print(json.dumps({"generated": s["generated"], "current": s["current"], "gates": {g["id"]: [g["met"], g["of"], g["complete"], g["behind"]] for g in s["gates"]}}))' \
    "$OUT/status.json" >>"$OUT/status-history.jsonl"
  echo "report: $f (current gate $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["current"])' "$OUT/status.json"))"
else
  rc=$?
  rm -f "$f.tmp"
  echo "$(date -u -Is) FAILED rc=$rc: $(tail -1 "$OUT/last-error.log")" | tee -a "$OUT/failures.log" >&2
  exit "$rc"
fi
