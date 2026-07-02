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
- Reconcile-history tuner is in **shadow mode** (logs a proposed adaptive interval; fixed 15 min stays live)
  pending review — flip `RECONCILE_MODE=active` only if the proposed data looks sane.
- Real configs live in `../instance/`; commit only `*.example`/`.tmpl`.
