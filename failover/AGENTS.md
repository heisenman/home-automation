# failover/ — cluster HA (dictator ⇄ warm standby)

Keeps a warm standby able to seize the dictator role. VIP `.200`; primary `.210`, standby `.245`.
Concepts + operations: [README.md](README.md), [failover-runbook.md](failover-runbook.md).

## Pieces

| File | Role | ADR |
|------|------|-----|
| `keepalived.conf.tmpl` | VRRP: floats the VIP; MASTER/BACKUP transitions | — |
| `notify.sh` | keepalived transition hook (MASTER/BACKUP side-effects) | — |
| `reconcile-history.sh` | Bidirectional windowed `hot.db` merge over SSH (proactive `--loop`) | 0016 |
| `reconcile-parquet.sh` | Row-level parquet deep-reconcile (dedup, zstd, rebuilds hash manifest) | 0018 |
| `provision-peer.sh` | Seed a peer: config → hot → archive → **hard gate** | 0018 |
| `sync-standby.sh`, `primary-watch.sh`, `healthcheck.sh` | Sync / watch / health | — |
| `cluster-doctor.sh` | Full cluster health check (convergence, archive completeness) | 0016,0018 |
| `failover-drill.sh` | Reversible, dry-runnable standby-seizure rehearsal | — |
| `dictator-files.manifest`, `cluster.env.example` | What replicates; cluster config template | — |

## Contracts & gotchas

- **⚠️ `.245` is the CRITICAL FILESERVER** — a live drill makes it briefly act as controller; that's **gated
  on Hugh + a window.** Building/dry-running the harness is not gated.
- Reconcile services are **VIP-gated** (only the active dictator runs the proactive loop).
- Reconcile-history tuner is in **shadow mode** (logs a proposed adaptive interval; fixed 15 min stays live).
  Week-long review DONE 2026-07-03 = SANE (proposed floored at 120 s, `D`≤4 s) — see
  [docs/reviews/2026-07-03-reconcile-shadow-tuning.md](../docs/reviews/2026-07-03-reconcile-shadow-tuning.md).
  Flip to `RECONCILE_MODE=active` is **staged for the new-NUC dictator bring-up** (reconcile pair 210⇄new-NUC),
  not the current 210⇄.245 pair.
- Real configs live in `../instance/`; commit only `*.example`/`.tmpl`.
