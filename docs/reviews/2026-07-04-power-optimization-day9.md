# Review — power-optimization campaign (day-9 readout)

**Date:** 2026-07-04 · **Reviewer:** dev · **Task:** `power-optimization` · **Doc:** [power-optimization.md](../power-optimization.md)
**Window:** 2026-06-25 → 2026-07-04 (~9.7 days, **2705 samples**, `ha-power-sampler.timer` 5-min cadence, `/var/log/ha-power`)
**Verdict:** ✅ **Campaign mature — idle at platform floor, no pathology, no software lever left. Recommend concluding the active-optimization phase.**

*(This is the scheduled day-7 readout, run at day-9 — the campaign kept sampling cleanly past the milestone.)*

## Numbers
| Metric | Value |
|---|---|
| Bucketing | 2698 idle (<10% busy) / 5 active — box is idle **99.7%** of the time |
| Idle package power | **mean 4.82 W · median 4.60 · min 3.74 · p95 6.51 · max 8.87** |
| Idle C2 residency | **mean 98.0%** (deep sleep dominant) |
| Temp | mean 47.0 °C · 41.9–54.9 (cool, no throttle risk) |
| Emergent power spikes (`events.log`) | **0** over 9.7 days |
| Recompile candidate (§2.7) | **none** — top CPU is `python3` (the stack) + transient `curl`/`ssh`/`sqlite3`/`systemctl`; nothing compute-bound + non-dispatched |

## The one real signal: +0.8 W idle drift — attributed, benign
Idle mean rose first-half 4.41 → second-half 5.22 W. Per-day it's not mysterious:

| | 06-25→29 | 06-30 | 07-01 | **07-02** | 07-03 | 07-04 |
|---|---|---|---|---|---|---|
| idle W mean | ~4.4 | 4.76 | 4.99 | **5.71** | 5.46 | 5.20 |
| idle C2 % | ~98.4 | 98.2 | 97.7 | **96.6** | 97.6 | 98.1 |

The rise tracks **work added to this box during the window**, not degradation:
- **07-02 is the peak** — the heaviest dev day (fleet-wide modular OTAs, SGP-40 gas node, ADR-0023 reach-census, S31 induction) all *building/OTA'ing/deploying on this dev+dictator box*. C2 dipping to 96.6% that day = active dev, not a stuck state.
- **New always-on services** landed mid-campaign and stayed: `ha-rollup.timer` (5-min), `event-reconcile` dispatcher, reach-census push, S31/gas MQTT bridges → more frequent brief wakeups.
- **The floor is essentially unchanged** (min 3.74–3.9 W throughout), and C2 recovered to ~98% once the 07-02 burst ended. Deep-sleep behavior is intact; there are simply *more* periodic wakeups now.

**Conclusion:** idle is at the platform floor; the mean rise is explained workload growth, not a leak. No anomaly to chase.

## Levers — what's left (little)
- **Recompile / source-build:** ruled out again (§2.7) — no compute-bound hot path. Measure-first was right.
- **BIOS (was the biggest lever):** **unavailable on this platform** — the G11 BIOS exposes no CPPC (`amd_pstate` n/a) and ASPM isn't OS-settable ([ha-dev-bios-no-cppc], 2026-07-02). So the headline software/BIOS lever is off the table; idle can't go meaningfully lower here.
- **Marginal, real:** the added background cadence is now the only dial. The **power-sampler itself runs every 5 min purely for this campaign** — now that it's characterized, relax it (5 → 15 min) to shed its own footprint. `ha-rollup.timer` 5-min is functional; leave it. *(Unit-cadence change = a gated edit — recommend, don't self-deploy; hand Hugh.)*

## RAPL → wall calibration (bonus — partly closed for free)
The pending Kill-A-Watt item is partly satisfied: the **S31 (`plug_g11`) now meters G11 wall power** (~10 W per prior obs) against RAPL package ~4.8 W ⇒ ~2× — the expected gap (PSU losses + RAM/NIC/NVMe/board outside package RAPL). A live read returned null just now (bridge/field — worth a quick confirm), but the S31 gives a standing wall reference without the Kill-A-Watt.

## Recommendation
1. **Conclude the active-optimization phase** — idle is at floor (4.8 W mean / 3.74 W min, 98% C2), stable, no pathology, no software lever remaining on this hardware.
2. **Relax `ha-power-sampler.timer` 5 → 15 min** (campaign characterized) — hand Hugh the one-line unit edit; keep low-cadence passive sampling for regression-watch.
3. **Move to the ledger/productization deliverable** (docs/power-optimization.md) — record the 9-day numbers as the box's characterized baseline; the S31 is the ongoing wall-power watch.
4. **Kill-A-Watt** stays a nice-to-have, no longer blocking (S31 covers wall).
