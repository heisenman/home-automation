#!/usr/bin/env bash
# Install + enable the COMPLETE dictator maintenance-timer set (idempotent). Run on the dictator, repo root.
#
# Root cause this fixes (2026-07-10): ha-2 was provisioned WITHOUT ha-rollup — so rungs.db was never built
# and the derived tier sat stale/unmaintained — plus the other derived/maintenance timers. There was no
# single source of truth for "what a self-sufficient dictator must run on a schedule," so a fast bring-up
# silently omitted them. This script IS that source of truth, so a (re)build can't drop them again.
# See docs/airgap/DATA-INTAKE-findings.md and memory prod-self-sufficient-is-the-goal.
#
#   ha-rollup              5min    ADR-0022 rung ladder — panel/standby history. CRITICAL (was missing).
#   ha-compactor           daily   hot.db -> parquet archive.
#   ha-gap-watcher         daily   history backfill.
#   ha-verify-hashes       weekly  parquet integrity (ADR-0004).
#   ha-power-sampler       15min   derived power metrics (data-storage-is-primary).
#   ha-gas-quality-sampler 60s     derived gas air-quality metric.
#   ha-pending-sweeper     15min   resolve migration pending-holds (ADR-0028).
#
# Conditional — NOT enabled here; enable per deployment:
#   ha-weather.timer         ONLINE dictators only (air-gap leaves OFF — no internet to fetch).
#   ha-router-reconcile.timer  air-gap-router deployments only.
#   ha-mesh-probe.timer        optional mesh diagnostics.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CORE="ha-rollup ha-compactor ha-gap-watcher ha-verify-hashes ha-power-sampler ha-gas-quality-sampler ha-pending-sweeper"

for u in $CORE; do
  sudo cp "$REPO/systemd/$u.service" "$REPO/systemd/$u.timer" /etc/systemd/system/
done
sudo systemctl daemon-reload
for u in $CORE; do sudo systemctl enable --now "$u.timer"; done

echo "enabled core maintenance timers: $CORE"
systemctl list-timers "ha-*" --all --no-legend --no-pager | awk '{print "  "$NF}' | sort -u
