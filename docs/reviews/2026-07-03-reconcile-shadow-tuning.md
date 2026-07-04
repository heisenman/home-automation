# Review — reconcile-history shadow-tuning (ADR-0016)

**Date:** 2026-07-03 · **Reviewer:** dev (Claude) · **Scheduled by:** cloud routine `trig_01WsViJjLPtMsu3i93qqKCk4` (reminder, due 2026-07-02)
**Subject:** ADR-0016 §2 "measured-adaptive interval, shadow-first" — the week-long shadow bake before flipping `RECONCILE_MODE=active`.
**Verdict:** ✅ **SANE — safe to activate.** Flip **deferred** (not for data reasons — see Sequencing).
**Relation to prior:** this is the scheduled read-out that follows ops's **Review 1 (2026-07-02)**, recorded inline
in [ADR-0016](../adr/ADR-0016-failover-history-reconciliation.md) — same verdict; this pass adds the new-NUC
re-sequencing (below) and is logged there as **Review 2**.

---

## What was reviewed
`reconcile-history.sh` runs the bidirectional `hot.db` merge on a fixed 15 min (`RECONCILE_INTERVAL_S=900`) active
cadence. In `RECONCILE_MODE=shadow` it *also* computes and logs a **proposed** adaptive interval each cycle without
applying it. The proposed value is `clamp( max(D/δ, I_min), I_min, I_max )` with `D`=measured merge duration,
`δ`=8% duty target, `I_min`=120 s, `I_max`=900 s (the loss budget). Action item per ADR-0016: after ~1 week,
read the log → sane ⇒ flip `active`; weird ⇒ revisit the tuner.

Source: `instance/ha-reconcile-tuning.log` — **767 samples spanning 8.01 days** (2026-06-25T04:59 → 2026-07-03T05:15).

## Data
| Field | Observed | Read |
|---|---|---|
| `proposed` | **120 s — 767/767 (100%)** | pinned at the `I_min` floor, every cycle |
| `D` (merge duration) | 1 s ×310, 2 s ×317, 3 s ×124, 4 s ×16 (max 4 s) | merge is trivially cheap |
| `rows_recent` | min 528 / avg 1096 / max 1969 | steady live ingest, no anomalies |
| coverage | 1 gap of 38 min (single missed run), else on-cadence | healthy loop |
| `mode` | `shadow` throughout | as intended; active cadence never moved |

## Interpretation
The duty-implied interval is `D/δ ≈ 2 / 0.08 ≈ 25 s`, well under the 120 s floor, so the tuner clamps up to `I_min`
on **every** cycle. That's why `proposed` is a flat 120 s for the whole week: the tuner is saturated at the floor,
saying *"reconcile costs ~2 s; run me as often as the anti-thrash floor allows."* This is the healthy signal, not a
stuck one — there is no regime where it would propose anything higher unless `D` grew toward `I_max/2` (450 s), the
`cluster-doctor` red-flag threshold. Measured `D` (≤4 s) is **~100× clear** of that line.

**Conclusion:** the shadow data is sane and stable. Waiting longer yields zero new information — the series has been
a constant 120 s for 767 consecutive samples. Activating would move the cadence 900 s → 120 s (7.5× more frequent),
shrinking the sudden-death loss-at-risk window from 15 min to 2 min at ~1.7% duty. No technical objection.

## Sequencing — why the flip is deferred (not the data)
Two reasons unrelated to the tuner:

1. **Standby topology is about to change.** A new NUC is inbound; the plan (Hugh, 2026-07-03) is to **fail over to
   the new NUC as the next dictator** once it's provisioned, rather than drill onto `.245` (the critical fileserver).
   The reconcile pair therefore becomes **210 ⇄ new-NUC**, not 210 ⇄ .245. Flipping `.245` to `active` now is churn
   on a box that's about to be relieved of standby duty.
2. **The failover path hasn't been drill-proven yet** (`failover-drill` still gated on Hugh + a window). The reconcile
   cadence only bites at the moment of a seizure; changing it before the base path is exercised means tuning a knob
   nothing has stressed.

**Carry-forward action:** enable `RECONCILE_MODE=active` (and optionally set `RECONCILE_INTERVAL_S=120` from the
observed proposed) as a step in the **new-NUC dictator bring-up**, when the 210 ⇄ new-NUC reconcile pair is
established — validated by the first failover-drill on that pair. One touch, right box, base path proven first.

This closes the scheduled review item; the flip is staged, not open-ended.
