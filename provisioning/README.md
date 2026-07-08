# Home Automation Server — Provisioning (GMKtec G11)

Build a new, eventually **air-gapped** home-automation server on **GMKtec NucBox G11**
hardware, reproducibly, with all OS + software updates delivered by **sneakernet** (USB).

This directory is the single source of truth for standing up a box from bare metal. It is
written so that **a fresh LLM instance with shell access on the target machine** can execute
it end-to-end.

> **Provisioning the second (failover) box? Read [`05-as-built-reference.md`](05-as-built-reference.md)
> first.** `01`…`04` were written before/during the first build and still carry speculative language;
> `05` is a live snapshot of the running `.210` dictator (confirmed hardware, exact package set, full
> service inventory, connection paths, and the keepalived/failover topology) so the next box comes up
> matching production instead of re-discovering it. Captured 2026-07-08.

---

## Target hardware (confirmed: Amazon ASIN B0CXSRR796)

| Component | Detail | Linux implication |
|---|---|---|
| CPU | AMD **Ryzen Embedded R2514** — 4C/8T Zen+, ~2.1–3.7 GHz | `amd64`, `amd64-microcode`, no Intel ucode |
| GPU | Radeon Vega (integrated) | headless; `amdgpu`/`radeon` in `linux-firmware` for console only |
| RAM | DDR4 SO-DIMM (16 GB in this SKU; up to 32 GB) | plenty for this workload |
| Storage | **2× M.2 2280 PCIe 3.0** NVMe (this SKU shipped with **one** populated) | Split/mirror (spec §3) only if a 2nd NVMe is present; the `.210` build ran single-disk. See `05` §A. |
| LAN | **Dual 2.5GbE Realtek RTL8125** `[10ec:8125]` — **confirmed** | in-tree **`r8169`** (Debian 13 kernel 6.12), **stable for weeks — no DKMS**. Primary `enp4s0`. See `05` §A. |
| Wi-Fi/BT | Onboard **MediaTek MT7922** (WiFi6E + BT5.2) | Wi-Fi unused (server is wired). **BT is in use — see below.** |
| BLE radio | **Onboard MT7922 Bluetooth** (`hci0`) — proven in production | **As-built reality overrides the original "use a dongle" plan:** the onboard MT7922 BT has run the passive scanner for weeks with no stalls. Needs **`firmware-mediatek`**. The **TP-Link UB500 (RTL8761B)** dongle is a known-good *fallback* only. See `05` §A. |

> **Onboard MT7922 BT — the plan changed.** The original caution (avoid MediaTek, fit the UB500 dongle)
> was written before testing. On the actual `.210` box the onboard MT7922 Bluetooth carried the BLE
> scanner reliably for weeks, so **no dongle was fitted**. It only requires the `firmware-mediatek`
> package. Keep the UB500 on hand as a fallback if a given box's onboard BT misbehaves. Onboard Wi-Fi
> stays down (server is wired). Full detail in `05-as-built-reference.md`.

---

## Distro decision: **Debian 13 "trixie" minimal (amd64), stable base + selective source-compile**

**Stable binary baseline, leaner than Ubuntu, glibc (not musl).** Same `apt`/glibc/systemd family as
production (.245) so the stack ports cleanly, but without snapd/cloud weight — a `netinst` +
minimal-tasksel install is a few hundred MB. Debian 13 ships **kernel 6.12** (newer than Ubuntu
24.04's 6.8) → better support for the new AMD R2514 + RTL8125. Snapshot-pinnable mirrors
(`snapshot.debian.org`) give bit-for-bit reproducibility across prime + failover and over time.

**Selective source-compile** (the parts where tuning / version-control pay off): the **kernel**
(optional `znver1` tune), **BlueZ** (control the `--experimental` passive-scan path), **mosquitto**.
Everything else — including **Python** — comes from Debian's frozen, snapshot-pinned binaries.

**Why not Alpine** (despite its leanness): musl libc breaks the pre-built binary wheels this stack
depends on (`duckdb`, `pyarrow`, `numpy`, `bleak`). Debian minimal gives Alpine-class footprint on
glibc without that fragility.

**Python ABI note.** The pinned wheelhouse (`../requirements.txt`) was captured on **cp312**; Debian
13's system Python is **3.13 (cp313)**. Default plan: use 3.13 and **rebuild the wheelhouse for cp313**
on the connected twin (one bump: `pyarrow 17→18`). Purist alternative: source-compile CPython 3.12.3
to keep the wheelhouse identical to .245 — valid, but adds Python to the recompile-on-update list.

---

## Two-stage strategy

```
   ┌─────────────────────────────────────────────────────────────────────┐
   │ STAGE 1 — BOOTSTRAP ISO  (provisioning/01-bootstrap-iso.md)          │
   │   Unattended Debian install → SSH + git + Node + Claude Code +       │
   │   this repo cloned on-device. Goal: an LLM can drive the box.        │
   └───────────────────────────────┬─────────────────────────────────────┘
                                    │  (boot, SSH in, run `claude`)
                                    ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │ STAGE 2 — FULL SERVER SPEC  (provisioning/02-full-server-spec.md)    │
   │   Configure everything: drivers, storage, BlueZ, venv, app, systemd, │
   │   mosquitto, data migration, verification.                           │
   │   FAST PATH (no LLM): ./provisioning/stage2-finish.sh automates the  │
   │   §4–§7 package/BlueZ/venv/service work; see 04-post-install.md.     │
   └───────────────────────────────┬─────────────────────────────────────┘
                                    │
                                    ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │ ONGOING — SNEAKERNET UPDATES  (provisioning/03-sneakernet-updates.md)│
   │   Air-gapped OS/pkg/python/source/data updates via signed USB        │
   │   bundles, verified with the existing hash-manifest tooling.         │
   └─────────────────────────────────────────────────────────────────────┘
```

Stage 2 runs **with internet for the first unit** (fastest path to a working box), but every install
step lists its **offline equivalent**, so the identical document provisions the air-gapped failover
unit and all future rebuilds.

---

## Files here

| Path | What |
|---|---|
| **`05-as-built-reference.md`** | **Live snapshot of the running `.210` dictator — read first for the next build (hardware, packages, services, connection paths, cluster topology)** |
| `01-bootstrap-iso.md` | How to build & flash the Stage-1 bootstrap ISO |
| `02-full-server-spec.md` | The Stage-2 spec (full reference + on-device findings + LLM directive) |
| `stage2-finish.sh` | **One-shot, idempotent Stage-2 finisher — run once after first login (no LLM)** |
| `04-post-install.md` | What the finisher does, the manual steps left, and the "drive with Claude" prompt |
| `03-sneakernet-updates.md` | Air-gapped update architecture (OS, pip, source, data, git) |
| `autoinstall/preseed.cfg` | Debian 13 installer preseed (unattended install) |
| `bootstrap/firstboot.sh` | First-boot provisioner (Node + Claude Code + clone) |
| `bootstrap/build-seed-iso.sh` | Remaster the official Debian netinst ISO with the preseed |
| `../requirements.txt` | Pinned Python deps (basis for the offline wheelhouse) |

## Failover unit (second G11)

**The second G11 has arrived (2026-07). It becomes the new record-keeper / next dictator, paired with
`.210` (not a `.245` drill).** Provision it from the **same** Stage-1 ISO + Stage-2 spec, add the
host-level infra layer (keepalived / chrony / ntfy / `firmware-mediatek` — see
[`05-as-built-reference.md`](05-as-built-reference.md) §G), then elevate it with
**`failover/provision-peer.sh --from 192.168.0.210`**, which syncs config + hot tier + the months-deep
parquet archive and **hard-gates on archive parity** before the box is eligible to hold the VIP. The
cluster topology (keepalived VRRP, VIP `.200`, MASTER/BACKUP priorities, `notify.sh`) lives under
`failover/`; `05` §F walks the peer bring-up end-to-end. `.245` retires from the HA role once the new
box is validated.
