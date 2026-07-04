# DEVICE-INTAKE.md — how to bring a new device into the system

**What this is.** The repeatable method for taking a device from "a board on the bench" to "a conformant,
reuse-maximizing, tested member of the system." It **operationalizes the four principles** ([AGENTS.md](AGENTS.md))
by pointing you at the right doc at each stage. It is the *planning* method; the mechanical flashing recipes
(ESPHome/local-flash) live in [SKILLS.md](../SKILLS.md) and the reference intake notes — this sits **above** them.

**Why a method.** Devices are complex bundles of abilities (the D1001 is edge-relay + display + battery +
data-of-record + BLE-gateway…). Intake done ad-hoc under-reuses, misses contracts, and forgets what the
hardware could do. This method makes intake **discover-driven**: what does it do → what must it obey → what
already exists → what's the thin new glue → prove it → register it.

> The three navigation axes this method uses: **by-location** ([AGENTS.md](AGENTS.md) tree, ADR-0021) ·
> **by-capability** ([REUSE.md](REUSE.md), ADR-0025) · **by-contract** ([CONFORMANCE.md](CONFORMANCE.md)). Plus
> the build-linkage map ([edge/MATRIX.md](../edge/MATRIX.md)). Intake is a walk across all four.

---

## The intake flow

### Stage 0 — Docs-first hardware survey  → *the authoritative docs*
Before asserting a single pin, register, or part, **read the schematic + vendor BSP/datasheet** (Principle 2).
Produce the honest hardware surface: MCU(s), radios, sensors, actuators, storage, power, buses. **Note what's
on-board but unused** — that becomes the roadmap lens (below). Can't find the doc? Ask; never infer a hardware
fact. *(Precedent: a D1001 audio ADC was mis-ID'd as a current monitor from inference alone.)*

### Stage 1 — Capability survey  → *[CONFORMANCE.md](CONFORMANCE.md) ability catalog (A–K)*
Walk the ability catalog against the device, in two passes:
- **Will-exercise:** which abilities does this device *do* in the system? (relays adverts? renders a display?
  battery-backed? acts on signed directives?)
- **Could-exercise (the roadmap lens):** which abilities does its *hardware* enable that you're not using yet?
  This is where Stage 0's "on-board but unused" list pays off — it becomes the enhancement backlog.

Output: the device's **ability set** (the will-do list), and a **could-do backlog**.

### Stage 2 — Conformance mapping  → *[CONFORMANCE.md](CONFORMANCE.md) SHALL items*
For each ability in the will-do set, read its **Binds** ADRs and **SHALL** items. The device's obligations are
the **union**. Also record the abilities it deliberately **does not** exercise, and why — that's how you avoid
over-binding (e.g. a display panel that isn't a signing node doesn't inherit the freshness/clock requirement).
Output: the **obligation set** — the checklist the device must satisfy to ship.

### Stage 3 — Module leverage (reuse-first)  → *[REUSE.md](REUSE.md) + [MATRIX.md](../edge/MATRIX.md) + `firmware/components/`*
For each ability, find what already implements it. `REUSE.md` is the by-capability index; `MATRIX.md` shows
which builds already link which module (a new device is a **column, not a fork** — ADR-0020). **Reuse or
justify in your commit** (Principle 1). Most abilities already have a module (`ha_battery`, `ha_ble_scan`,
`ha_reach`, `fs_ops`, `ha_replica`, `switchbot_decode`…) or a server contract (`vm.controls`, `/rung/since`,
the edge event envelope) — reaching for those instead of rebuilding is the whole point.

### Stage 4 — Decomposition (before any code)  → *[ADR-0020](adr/ADR-0020-shared-edge-panel-firmware-core.md) / [firmware/AGENTS.md](../firmware/AGENTS.md)*
**HARD LINE (Principle 3): name the seams before writing code.** For each ability, split into **shared module**
(a real `firmware/components/<m>` — platform differences become injected cfg/hooks) vs **thin device glue**
(this build's wiring). New shared capability → new module; app-local → an in-app unit. Output: the module list
+ boundaries, as a plan/ADR. *No decomposition, no dev.*

### Stage 5 — Implementation  → *additive-first*
Build the new shared module and **prove it in isolation** before pointing the device at it. Live/edge nodes
migrate gated, one at a time, re-validated. Device glue is thin: platform shim + REQUIRES-picked modules +
config. Honor every standing contract (secrets never in git; dumb-relay; gated production writes).

### Stage 6 — Testing & validation  → *host tests + on-device + conformance verify*
- **Host test** every pure module (`test/run.sh`, no IDF) — correctness proven off-target.
- **On-device** validate each ability against reality (readback, not assumption — ADR-0014 R3).
- **Conformance verify:** walk the Stage-2 obligation set — does the device meet each SHALL? Record gaps
  explicitly (a known, deliberate gap is fine; a silent one is a defect).

### Stage 7 — Registration & maps  → *`instance/devices.yaml` + generators*
Register the device (`device_id`, `area`, `device_type`, `roles`, `capabilities`) — the **dictator owns the
registry** (ADR-0001). Regenerate the code-backed docs so nothing rots: `tools/gen_module_matrix.py --write`
([MATRIX.md](../edge/MATRIX.md)), `tools/gen_reuse.py --write` if you added a shared module ([REUSE.md](REUSE.md)).
Update the coord board / FOLLOWUPS. *(Enforcement-next: a generated per-device conformance matrix from declared
abilities × [CONFORMANCE.md](CONFORMANCE.md) — see that doc's Enforcement section.)*

---

## The roadmap lens (Stage 1, could-do pass)

Intake isn't only "wire up what it does today." The most valuable output is often the **could-do backlog**: the
abilities the hardware enables that aren't used yet. Surface it by intersecting Stage 0's "on-board but unused"
list with the ability catalog, then rank by value × effort × dependency. This turns a one-time intake into a
living roadmap. *(Worked example: the D1001 review found an unused RTC, IMU, codec, and camera → a seven-item
roadmap — see [d1001-capability-roadmap.md](design/d1001-capability-roadmap.md).)*

## Worked examples

- **D1001 (retrospective intake / review):** [d1001-capability-roadmap.md](design/d1001-capability-roadmap.md)
  is Stages 1–4 applied to a device already in service — surfacing both its conformance status and its could-do
  backlog. The canonical walkthrough of this method.
- **E1001 (intake in progress):** [design/e1001-roadmap.md](design/e1001-roadmap.md) +
  [design/e1001-epaper-renderer.md](design/e1001-epaper-renderer.md) — Stage 0 (schematic-confirmed platform),
  Stage 1–4 for the display/sensor/battery abilities, reusing the shared BFF spec (E) and `ha_battery` (F).

## The one-screen checklist

```
0. HARDWARE      read schematic + BSP; list every peripheral; flag on-board-but-unused
1. CAPABILITY    walk CONFORMANCE.md A–K: will-do set + could-do backlog
2. CONFORMANCE   each will-do ability → its SHALL items; obligations = the union; note the deliberate NOTs
3. REUSE         REUSE.md + MATRIX.md + firmware/components/ → reuse or justify (a device is a column, not a fork)
4. DECOMPOSE     name shared modules vs thin glue BEFORE code (ADR-0020). No decomposition, no dev.
5. IMPLEMENT     build+prove shared module in isolation; thin glue; gated/additive
6. TEST          host test pure modules; on-device readback; verify every SHALL (record gaps)
7. REGISTER      devices.yaml (id/area/type/roles/caps); regen MATRIX.md + REUSE.md; update board
```

*See also:* [AGENTS.md](AGENTS.md) (the four principles) · [CONFORMANCE.md](CONFORMANCE.md) ·
[REUSE.md](REUSE.md) · [firmware/AGENTS.md](../firmware/AGENTS.md) · [SKILLS.md](../SKILLS.md) (flashing recipes).
