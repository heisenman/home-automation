# Power / Idle Optimization — plan, campaign, and box-productization procedure

**Date:** 2026-06-25 · **Author:** dev (210) · **Status:** DRAFT for review by **ops** + **Hugh**
**Scope:** maximize time spent in deep idle (C2/package-idle) on the dictator box under home-automation
constraints, **and** turn the findings into a repeatable procedure — a tuned install image, a set of
bring-up directives, and a living results ledger — for the next boxes (likely the **same** AMD Ryzen
Embedded hardware, but designed to **detect-and-adapt** when a box diverges).

This is the power-focused successor to `docs/decisions/os-service-optimization.md` (ops's footprint pass:
"services are cheap, do a loading-reduction pass" — it disabled unused `ipvsadm`; little else on 210).
That pass was about RAM/service count; **this** is about energy/idle-residency and battery-backup runtime.

> **Constraint that governs everything here:** 210 is the **live dictator**. It must keep: continuous BLE
> passive scan (the only source for some sensors), MQTT broker, the controller loop, keepalived VRRP +
> heartbeat (failover sensing), and edge mapper/history. **No optimization may risk control latency,
> failover detection, or sensor freshness.** Every lever below carries an explicit HA-constraint check.

---

## 1. Measured baseline (210, 2026-06-25, read-only)

| Dimension | Observed | Implication |
|---|---|---|
| **SoC** | AMD **Ryzen Embedded R2514** — 4C/8T, Zen+ (fam 17h), Radeon iGPU; max 2.1 GHz, min 1.4 GHz, **CPB boost** seen to ~2.8 GHz | Configurable-TDP embedded part; real headroom; idle behaviour matters more than peak. |
| **cpufreq** | driver `acpi-cpufreq`, governor `schedutil`; **no `amd_pstate`**, **no `acpi_cppc`** sysfs | `amd_pstate` (EPP-based, more efficient) is **not active** — CPPC appears off in BIOS or unsupported on Zen+. Top lever to investigate. |
| **idle** | driver `acpi_idle`; states **POLL / C1 / C2** only; **~95% of uptime in C2** (10.1 h of 10.6 h at first read; lifetime avg incl. idle hours) | Already idles deep & often. No deeper OS C-state than C2 exposed → any deeper *package* idle is BIOS-gated, not OS. |
| **idle (active)** | C2 residency drops during active dev sessions (expected) | Campaign must separate **active vs idle** residency, not just report the lifetime average. |
| **iGPU** | `power_dpm_state=performance`, `force_performance_level=auto` | Headless box (no display-manager installed); iGPU need not hold high clocks. Reversible sysfs win. |
| **default target** | **`graphical.target`** (but **no** DM/Xorg/Wayland installed — empty hull) | Wrong default for a headless server; set `multi-user.target` in the image (hygiene; avoids pulling graphical deps on rebuild). |
| **kernel cmdline** | `ro quiet` only — **no power params** | Clean slate for tuned boot params (see §2). |
| **timers** | `apt-daily`, `apt-daily-upgrade`, `man-db`, `logrotate`, `fstrim`, `e2scrub`, `dpkg-db-backup` + our `ha-*` | `apt-*`/`man-db` are pointless wakeups on an (eventually) air-gapped, deliberately-updated box. |
| **top IRQ rates** | CAL ~651/s (function-call IPIs), LOC ~380/s aggregate (tickless residual), enp4s0 ~41/s, xhci ~12/s (BLE radio on USB), RES ~7/s | NIC + USB-BLE are inherent to the HA function. **CAL/IPI rate is the most interesting unknown** → campaign target. |
| **peripherals** | 5 USB devices all `autosuspend=auto` (good); NVMe sched `[none]` (good); NVMe runtime PM `unsupported` (device-managed APST only) | USB/NVMe already reasonable; verify NVMe APST with `nvme-cli` during campaign. |
| **mem** | 1.1 / 11.9 GiB used, 6.3 GiB cache, **swap 11 GiB untouched**, no zram/zswap | Memory is a non-issue for power; ignore. |
| **RAPL** | `intel-rapl` powercap present (AMD exposes it) but `energy_uj` is **root-only (0400)** | We **can** measure real package energy — needs a small root reader (campaign §3). |

**Headline:** the box is already a good idle citizen (~95% C2, fast wake). Gains are therefore *specific and
measured*, not a big sweep — and the **bigger prize is the reproducible procedure** (§4), so box #2…#N
start already-tuned instead of being hand-dialed.

---

## 2. Optimization catalog

Each lever: **mechanism · expected effect · risk/reversibility · HA-constraint check · apply · verify ·
portability**. Tier: **🟢 bring-up directive** (safe default, bake into image) · **🟡 measure-first**
(apply + A/B in the campaign) · **🔴 BIOS / Hugh hands-on**.

### 2.1 Firmware / BIOS  🔴 (Hugh hands-on — needs console at boot)
- **Enable CPPC** ("Collaborative Processor Performance Control", sometimes under CBS/AMD Overclocking).
  *Effect:* makes `amd_pstate` usable → finer, more efficient DVFS than `acpi-cpufreq`. *Verify:* after
  reboot `/sys/devices/system/cpu/cpu0/acpi_cppc/` appears. *Portability:* capability-gated — the tuning
  script tries `amd_pstate` only if CPPC is present, else stays on `acpi-cpufreq`+`schedutil` (correct
  fallback). *Risk:* none beyond a reboot; fully reversible in BIOS.
- **Power Supply Idle Control → "Low Current Idle"** (AMD CBS). *Effect:* permits deeper package idle
  (the C1/C2-only exposure suggests deep pkg idle may be gated). *Risk:* on some desktop PSUs causes
  idle-hangs; on an embedded board with an integrated supply this is typically safe — **verify stability
  in the campaign before baking in.** *Verify:* watch for idle stalls + measure pkg power delta.
- **Disable unused on-board devices** (audio, extra NIC, serial, etc. if present). *Effect:* fewer powered
  rails / fewer wakeup sources. *Portability:* document per-board; detect via `lspci`/`lsusb` in the script.

### 2.2 Kernel boot params  🟡 (one A/B each; bake the winners into the image)
- `amd_pstate=guided` (or `=active` for EPP) — **only if CPPC present**. A/B vs `acpi-cpufreq` on energy.
- `pcie_aspm=powersave` — policy is currently `[default]`; force L1 where the link allows. *HA check:* must
  not add NIC/USB latency that delays MQTT/heartbeat — measure round-trip + heartbeat freshness.
- Keep `quiet`; **do not** touch `mitigations=` (security posture stays).
- *Portability:* params live in a template the tuning script writes per-detected-capability, not a static
  blob.

### 2.3 cpufreq / idle governor  🟢/🟡
- 🟢 Keep **`schedutil`** (correct for this driver; race-to-idle friendly). If `amd_pstate=active` lands,
  switch to EPP `power`/`balance_power` and A/B.
- 🟡 **CPB / boost:** race-to-idle usually wins, so default = leave boost on. *Only* consider capping max
  freq when **battery runtime** is the active constraint — campaign will quantify the boost energy cost so
  the battery-mode profile is data-driven, not a guess.

### 2.4 iGPU (amdgpu)  🟢
- Headless → **`force_performance_level=low`** (caps iGPU clocks; keeps the driver + EFI framebuffer).
  *Effect:* removes iGPU dynamic-clock power with zero functional loss (no display, no compute use).
  *Apply:* udev rule / oneshot service writing the sysfs node at boot (idempotent). *Reversible:* set back
  to `auto`. *Verify:* iGPU clocks pinned low; pkg-power delta. *Portability:* gate on "no display in use".

### 2.5 Services & timers  🟢
- **Mask pointless-wakeup timers** for a deliberately-updated/air-gapped box: `apt-daily.timer`,
  `apt-daily-upgrade.timer`, `man-db.timer`. *(Updates become a deliberate maintenance action, not a
  background wake.)* *Reversible:* unmask. *HA check:* none — these are housekeeping.
- **`default.target = multi-user.target`** (drop the graphical hull). *Reversible:* `set-default graphical`.
- **Coalesce timer wakeups:** add `AccuracySec=` (e.g. 1min) + `RandomizedDelaySec=` to our `ha-*` timers
  and the OS housekeeping timers so several firings share one wake. *HA check:* fine for compactor/weather/
  gap-watcher (none are latency-critical). Do **not** loosen anything on the control path.
- 🟡 **Heartbeat cadence:** `ha-cluster-heartbeat` wakes every 3 s (freshness gate 12 s). Relaxing to 4–5 s
  cuts steady wakeups ~40% while staying inside a 12 s (×3–4 miss) detect window. **Couples to failover RTO
  (ROADMAP theme A)** — decide there, not unilaterally. *HA check: this is the one change that touches
  failover detection latency — measure both.*
- Carry forward ops's `ipvsadm` disable (already done) into the image base.

### 2.6 Peripherals  🟢/🟡
- 🟢 Keep USB autosuspend `auto`. **Exception to verify:** the on-board BLE radio (xhci) must **not**
  autosuspend mid-scan — confirm the scan keeps it active (it should, as an open HCI socket). Pin it
  `on` if the campaign ever shows scan gaps.
- 🟡 **NVMe APST:** confirm with `nvme get-feature -f 0x0c` whether autonomous low-power states are enabled;
  enable the deepest safe non-operational state whose exit latency doesn't stall the DB writers.

---

## 3. The 7–15 day profiling campaign (cost-aware by design)

**Goal:** find *emergent* higher-power moments — unexpected busy loops, cron/timer storms, a service that
wakes too often, the CAL/IPI mystery — that a one-shot snapshot misses. **The profiler must not defeat its
own purpose:** prefer reading *cumulative counters infrequently* over high-frequency sampling, and account
for the profiler's own footprint explicitly.

### 3.1 Layer 1 — cheap periodic counter sampler (always on for the window)
A tiny root timer fires **every 5 min** (1 wake / 5 min ≈ negligible vs the existing 3 s heartbeat) and
appends one CSV row of **deltas** of cumulative counters — counters are free to read; the delta gives the
interval's behaviour with no continuous polling:
- **RAPL** `energy_uj` (Δ ÷ Δt = **avg package watts** for the interval) — the real power signal.
- per-CPU **C-state `time`/`usage`** (Δ → **%C2 residency**, the idle KPI; split active vs idle here).
- `/proc/stat` (Δ → CPU busy %, per-mode user/sys/irq/softirq).
- `/proc/interrupts` (Δ → per-source IRQ **rate**, to watch CAL/LOC/NIC/USB drift).
- `loadavg`, `/sys/class/hwmon` **temps**, top-1 process by CPU (single `ps` snapshot).
- **Self-cost line:** the sampler reads its *own* RAPL delta around its run so the report can state "the
  profiler cost X mW-avg / Y CPU-seconds/day" and subtract/acknowledge it.
Storage: append-only CSV at `/var/log/ha-power/samples.csv` (+ `state.json` for the prev-cumulative
deltas), root-owned; ~a few KB/day (logrotate to be added at productization). **LIVE since 2026-06-25**
(`tools/power_sample.py` + `systemd/ha-power-sampler.{service,timer}`, 5-min `OnUnitActiveSec`, `Nice=19`/
`idle` sched so it never touches the control path).

### 3.2 Layer 2 — transient-event capture (near-free, catches what 5-min sampling misses)
- **BSD process accounting (`acct`/`psacct`):** the kernel writes one record per **process exit** (command,
  CPU time, elapsed). This catches *every* short-lived CPU spike between samples (e.g. "what ran at 03:14
  and burned a core") at near-zero overhead — the ideal complement to Layer 1. Review with `sa`/`lastcomm`.
- **journald + `systemd-analyze`:** correlate spikes with service/timer firings already logged.

### 3.3 Layer 3 — bounded deep dives (occasional, explicitly costed)
- **`powertop` capture once/day for ~90 s** (not continuous) → wakeup-source attribution + tunable report.
  Its cost is a known, bounded sample, logged as such.
- **Threshold-triggered burst:** if a Layer-1 interval exceeds a power/CPU threshold, the *next* few
  intervals sample at 1 min (adaptive) to characterize the event — so emergent spikes get resolution
  without paying for it the other 99% of the time.

### 3.4 Tooling cost
Installs needed (one-time, while apt still reachable pre-air-gap): `sysstat`, `acct`, `powertop`,
`linux-cpupower`, `nvme-cli`. All are dormant except when invoked. The Layer-1 sampler itself needs **no
packages** (pure `/sys` + `/proc` reads + a root RAPL read) — so it can start **tonight** and collect the
full window even before the deep-dive tools land.

### 3.5 Exit criteria
A campaign report: active-vs-idle %C2 + avg/95p package watts, the IRQ-rate timeline, the CAL/IPI
root-cause, a ranked list of emergent high-power events with attributed causes + proposed mitigations, and
the **profiler's own measured footprint**.

---

## 4. Productization — image, bring-up directives, divergent-hardware strategy

This is the point of the exercise: **box #2…#N should boot already-tuned.**

### 4.1 A hardware-detecting, idempotent tuning script
`provisioning/power-tune.sh` (new) — runs at bring-up and is re-runnable. It **detects then adapts**:
- read CPU vendor/family, CPPC presence, available govs, idle states, iGPU presence, display-in-use, NIC/
  USB inventory, NVMe APST support;
- apply only the levers the detected capabilities support (e.g. `amd_pstate` *iff* CPPC; iGPU-low *iff*
  headless + amdgpu), each behind a capability gate so a **divergent box degrades gracefully** to the
  correct fallback instead of mis-applying;
- write boot-param + sysfs + systemd-mask changes idempotently; print a before/after diff;
- `--dry-run` (read-only, prints what it *would* do) and `--revert` (undo to recorded prior state).
Wires into the existing flow alongside `provisioning/stage2-finish.sh` as a new gated step (and into
`provisioning/02-full-server-spec.md` / `04-post-install.md`).

### 4.2 The install image
Once the directives are proven on 210 (campaign-validated), capture them into a **reference image**:
multi-user default, masked housekeeping timers, tuned boot params (per-capability), the `power-tune.sh` +
sampler units pre-installed, ops's lean-base omissions (no cloud-init/dpdk/multipathd per the prior pass).
The image is the **same-hardware fast path**; `power-tune.sh` is the **divergence safety net** on top.

### 4.3 Living results ledger (§6 of this doc)
"Ongoing documentation of issues found, mitigations taken, performance gains achieved, and update
instructions" — kept as an **append-only table in §6**, one row per finding: date · issue · mechanism ·
mitigation · measured Δ (watts / %C2) · reversible? · baked-into-image? · applies-to (this box / all /
capability-gated). This is the artifact the next bring-up reads.

---

## 5. Phases, owners, decisions

**Phase 0 ✅ DONE (2026-06-25):** Layer-1 sampler live (no installs, root timer); the 7–15 day window is
**collecting now**. First live read: package ~11.8 W under active load, ~74–96% C2. *(dev)*
**Phase 1 (campaign, days 0–15):** Layers 2–3 tools installed; A/B the 🟡 levers; collect. *(dev)*
**Phase 2 (Hugh window):** the 🔴 BIOS items (CPPC, idle-control) at a console; re-measure. *(Hugh + dev)*
**Phase 3:** write `power-tune.sh` + fold winners into provisioning + draft the image. *(dev, ops review)*
**Phase 4:** capture the reference image; ledger reflects gains. *(dev + ops)*

**Decisions needed from Hugh:**
- [ ] OK to **start the Layer-1 sampler now** (zero installs, one root systemd timer, CSV under `instance/profiling/`)?
- [ ] When can you spend ~10 min in **BIOS** for the CPPC + idle-control toggles (Phase 2)?
- [ ] A one-time **wall-meter** reading (Kill-A-Watt) to calibrate RAPL(SoC-only) → wall power for battery-runtime math?
- [ ] Confirm the box will be **deliberately-updated** (so masking `apt-*`/`man-db` timers is correct)?
- [ ] Heartbeat-cadence relax (3 s→4–5 s) — decide alongside failover RTO in ROADMAP theme A?

---

## 6. Results ledger (living — append one row per finding)

| Date | Issue / observation | Mechanism | Mitigation | Measured Δ | Reversible | In image | Applies to |
|------|---------------------|-----------|------------|------------|-----------|----------|-----------|
| 2026-06-25 | Baseline captured (see §1) | — | — | ~95% C2 lifetime; pkg-W TBD (RAPL root) | — | — | this box |
| 2026-06-25 | Phase 0 sampler live | systemd timer reads RAPL/C-state/IRQ deltas every 5 min | n/a (measurement) | first read pkg ~11.8 W (active load); idle TBD over window | yes (`systemctl disable`) | yes (units belong in image) | all (capability-gated on RAPL) |
| _(campaign findings land here)_ | | | | | | | |
