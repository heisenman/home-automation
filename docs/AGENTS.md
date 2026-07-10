# docs/ — decisions, plans, runbooks

Where the "why" and the "how" live. Root orientation is [../AGENTS.md](../AGENTS.md); this explains how the
docs here are organized.

*↑ The by-location node for `docs/` in the [root AGENTS.md](../AGENTS.md) tree (ADR-0021/0025); the by-capability index is [`docs/REUSE.md`](REUSE.md). Link up, don't duplicate.*

## Structure

| Path | What it is | When to touch |
|------|------------|---------------|
| `adr/` | **Architecture Decision Records** (ADR-0001…). The authoritative decisions. | New/changed decision → add or update an ADR (Proposed → Accepted). |
| `decisions/` | Lighter-weight decision notes (below ADR bar) | Smaller calls |
| `ROADMAP.md` | Forward plan / phases | Planning |
| `FOLLOWUPS.md` | Live action-item ledger | **Reconcile at every checkpoint** |
| `CHECKPOINT.md` | The checkpoint routine (what to reconcile + verify) | Run at each checkpoint |
| `retro/` | Retrospectives | After milestones/incidents |
| `cutover/`, `CUTOVER-*.md` | Cutover procedures/history | Migrations |
| `device-onboarding.md`, `*-intake.md`, `*-protocol.md` | Device/protocol how-tos | Onboarding, protocol work |
| `airgap/AIRGAP-MIGRATION.md` (+ `MIGRATION-DESIGN-LOG.md`) | Air-gap migration: requirements, architecture, learnings + running journal | Any air-gap / device-migration work |
| `SECRETS.md` | Secrets **discovery index** — where each secret class lives (values stay gitignored) | Onboarding, credential hunts |
| `runbook-device-verification.md` | How to VERIFY a device's state/function (trust-but-verify, AGENTS §5) | Before relying on any device state |
| `DEVICE-MODEL.md` | The **object model** — Node / Ability / Entity, the by-structure nav axis (ADR-0034) | Reasoning about device classes, migration, or the registries |

## ADR index (authoritative decisions)

0001 dictator authority · 0002 capability contract (traits) · 0003 wasm firmware split · 0004 ext4 integrity ·
0005 tiered secure-boot · 0006 two-tier storage (sqlite+parquet) · 0007 device history sync · 0008 weather lane ·
0009 history continuity · 0010 command/control protocol · 0011 automation controller · 0011 modular infra ·
0012 comms/events abstraction · 0013 presentation architecture (BFF = UI truth) · 0014 device-control conventions ·
0015 edge-relay coverage · 0016 failover history reconciliation · 0017 API TLS + token auth · 0018 node
provisioning/record-keeping · 0019 screen-interface architecture (panels) · **0020 shared edge/panel firmware
core** · **0021 repo documentation tree (this convention)**.

## Discipline

- ADRs are the memory of *why*. Before proposing a change, check whether an ADR already governs it.
- Keep `FOLLOWUPS.md` and ADR **Status** lines truthful — reconcile at each checkpoint (`CHECKPOINT.md`),
  don't just commit code. The `AGENTS.md` tree (ADR-0021) is part of what a checkpoint verifies.
