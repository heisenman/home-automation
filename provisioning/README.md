# Home Automation Server — Provisioning (GMKtec G11)

Build a new, eventually **air-gapped** home-automation server on **GMKtec NucBox G11**
hardware, reproducibly, with all OS + software updates delivered by **sneakernet** (USB).

This directory is the single source of truth for standing up a box from bare metal. It is
written so that **a fresh LLM instance with shell access on the target machine** can execute
it end-to-end.

---

## Target hardware (confirmed: Amazon ASIN B0CXSRR796)

| Component | Detail | Linux implication |
|---|---|---|
| CPU | AMD **Ryzen Embedded R2514** — 4C/8T Zen+, ~2.1–3.7 GHz | `amd64`, `amd64-microcode`, no Intel ucode |
| GPU | Radeon Vega (integrated) | headless; `amdgpu`/`radeon` in `linux-firmware` for console only |
| RAM | DDR4 SO-DIMM (16 GB in this SKU; up to 32 GB) | plenty for this workload |
| Storage | **2× M.2 2280 PCIe 3.0** NVMe | NVMe-A = OS, NVMe-B = data (`instance/`) — see spec §3 |
| LAN | **Dual 2.5GbE** — almost certainly **Realtek RTL8125** | in-tree `r8169` (Debian 13 kernel 6.12) handles it well; keep `r8125-dkms` source in the offline bundle as fallback. **Verify with `lspci` on-device** — could be Intel i226 |
| Wi-Fi/BT | WiFi6E + BT5.2 (likely MediaTek MT7922) | **not used** — server is wired; BLE runs on the proven USB dongle below |
| BLE radio | **TP-Link UB500 (Realtek RTL8761B)** USB dongle — already owned & working on .245 | the BLE path. Onboard radio stays disabled to avoid the MediaTek-on-Linux risk we already hit |

> **Why ignore the onboard Wi-Fi/BT?** BLE is the critical path for this project and we already
> burned time on a MediaTek USB radio that exposed no BT interface on Linux. The server is wired
> (2.5GbE), so onboard Wi-Fi is unnecessary, and the RTL8761B UB500 dongle is known-good. De-risk
> by using it and leaving onboard radios off.

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
   │   The on-device LLM configures everything: drivers, storage, BlueZ,  │
   │   venv, app, systemd, mosquitto, data migration, verification.       │
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
| `01-bootstrap-iso.md` | How to build & flash the Stage-1 bootstrap ISO |
| `02-full-server-spec.md` | The Stage-2 spec the on-device LLM executes |
| `03-sneakernet-updates.md` | Air-gapped update architecture (OS, pip, source, data, git) |
| `autoinstall/preseed.cfg` | Debian 13 installer preseed (unattended install) |
| `bootstrap/firstboot.sh` | First-boot provisioner (Node + Claude Code + clone) |
| `bootstrap/build-seed-iso.sh` | Remaster the official Debian netinst ISO with the preseed |
| `../requirements.txt` | Pinned Python deps (basis for the offline wheelhouse) |

## Failover unit (second G11)

Buy **one** G11 first, validate it as primary, then buy the second as failover. The failover is
provisioned from the **same** Stage-1 ISO + Stage-2 spec, then kept current by applying the **same
sneakernet bundles** to both boxes. Data parity via periodic `rsync` of `instance/db/` from prime →
failover (pull model on the failover). Promotion = point the LAN's HA hostname/IP at the failover and
start its services. Details in spec §12.
