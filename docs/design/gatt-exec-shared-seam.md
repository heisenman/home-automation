# Design draft — promote `gatt_exec` to a shared component (`ha_gatt_exec`)

**Status:** DRAFT for review (Hugh + ops). No code changes yet. **Contract:** ADR-0020. **Task:** `edge-core-dedup` (P1 tail).
**Author:** dev, 2026-07-05. **Prereq reading:** [dedup-register.md](dedup-register.md), the `ha_gatt` component (the exact precedent).

## 1. Why this is the "hard one"
`gatt_exec.c` is byte-identical ×3 (c3/c6/s3-eth, 370 lines) — but unlike `ha_relay`/`ha_sntp` it is **not** a plain
promote, because it calls board-local code. The register's "promote as-is" label was wrong; this is a **cfg-seam
port**, exactly like `ha_gatt` (which was extracted from `gatt_history.c` the same way). The good news: the analysis
below shows only **two real seams**, and the risky capability (`HA_ALLOW_GATT_WRITE`) is currently **off on every
node**, so the extraction changes no live actuation.

## 2. Dependency analysis — what actually couples it to the board
Grep of every non-IDF, non-NimBLE symbol in `gatt_exec.c`:

| Board-local reference | Lines | Verdict |
|---|---|---|
| `ha_mqtt_publish_reply(reqid, payload)` (via `reply()`) | 99 | **SEAM** → `cfg.publish_reply` callback |
| `ha_mqtt_log(...)` | 352, 355, 362, 365 | **SEAM** → `cfg.log` callback |
| `ha_ble_scan_pause/resume`, `ha_ble_lookup_addr`, `ha_ble_own_addr_type` | 290, 354, 360, 363 | **NOT a seam** — already the shared `ha_ble_scan` API; just swap `#include "ble_scan.h"` → `"ha_ble_scan.h"` + `REQUIRES ha_ble_scan` |
| `secrets.h` (`#if __has_include`) | 19-21 | only supplies `HA_ALLOW_GATT_WRITE` (a compile-time gate) — **see §4, the one real decision** |

So the seam surface is **identical in shape to `ha_gatt`**: a `publish` sink + a `log` sink, installed once at boot.

## 3. Proposed component: `firmware/components/ha_gatt_exec`
Sibling to `ha_gatt` (history). Keep them separate: `ha_gatt` = SwitchBot history pull; `ha_gatt_exec` = generic
server-composed step interpreter. Layout mirrors the others (`CMakeLists.txt`, `ha_gatt_exec.c`, `include/ha_gatt_exec.h`
with the `BREADCRUMB:`/`REUSE-WHEN:` header).

```c
// include/ha_gatt_exec.h
typedef struct {
    void (*publish_reply)(const char *reqid, const char *json, void *user); // was ha_mqtt_publish_reply
    void (*log)(const char *msg, void *user);                              // was ha_mqtt_log; NULL => silent
    void *user;
} ha_gatt_exec_cfg_t;

void ha_gatt_exec_init(const ha_gatt_exec_cfg_t *cfg);   // *cfg copied; call once at boot
bool ha_gatt_exec_run(const char *reqid, const char *mac_str, const char *steps_json);  // was gatt_exec_run
bool ha_gatt_exec_busy(void);                            // was gatt_exec_busy
```

`CMakeLists.txt`: `REQUIRES bt ha_ble_scan json` (NimBLE host + shared observer + cJSON). No `ha_mqtt`.
Wire-format on `home/edge/<node>/<reqid>/reply` is **unchanged** (the component just builds the same JSON and hands it
to `cfg.publish_reply`; the edge callback forwards to `ha_mqtt_publish_reply`, which owns topic construction).

## 4. THE decision: `HA_ALLOW_GATT_WRITE` (needs Hugh's call)
Today writes are a **compile-time capability**: a telemetry node's binary literally cannot actuate — the write path is
`#if`-compiled out (least privilege / defense-in-depth). Extraction must preserve that boundary. Three options:

- **(A) Kconfig `CONFIG_HA_GATT_ALLOW_WRITE` (default n) — RECOMMENDED.** Component reads `#if CONFIG_HA_GATT_ALLOW_WRITE`.
  An actuator build flips it in its `sdkconfig.defaults`. Keeps the **compile-time** boundary (telemetry binaries have
  no write path), makes the capability visible in menuconfig, and is per-board clean. Cost: one Kconfig file in the component.
- **(B) Component build define.** Board CMake does `target_compile_definitions(... PRIVATE HA_ALLOW_GATT_WRITE=1)` for
  actuator builds. Same compile-time boundary, less discoverable, a bit hackier.
- **(C) Runtime `cfg.allow_write` bool.** Simplest, but **downgrades security**: the write path is compiled into every
  binary and gated by a runtime flag. A telemetry node could actuate if that bool were ever mis-set. **Not recommended.**

Default in all three stays **OFF**, and no node sets it today, so whichever we pick, current behavior is unchanged.

**DECIDED (Hugh, 2026-07-05): option (A) Kconfig** — `CONFIG_HA_GATT_ALLOW_WRITE`, default `n`. The component gates
the write path with `#if CONFIG_HA_GATT_ALLOW_WRITE`; an actuator build opts in via `CONFIG_HA_GATT_ALLOW_WRITE=y`
in its `sdkconfig.defaults`. Preserves the compile-time least-privilege boundary (telemetry binaries contain no write
path) and surfaces the capability in menuconfig.

## 5. Consumer changes — small and identical per board
Only `main/ha_mqtt.c` on each of c3/c6/s3-eth (verified: `app_main` does not reference `gatt_exec`). Mirror the
`ha_gatt` wiring already living in these files:
1. `#include "gatt_exec.h"` → `#include "ha_gatt_exec.h"`.
2. In `ha_mqtt_start`, add `ha_gatt_exec_init(&(ha_gatt_exec_cfg_t){ .publish_reply = edge_exec_reply, .log = edge_gatt_log });`
   with a one-line `edge_exec_reply` that calls `ha_mqtt_publish_reply` (reuse the existing `edge_gatt_log`).
3. Rename call-sites: `gatt_exec_run` → `ha_gatt_exec_run`, `gatt_exec_busy` → `ha_gatt_exec_busy`
   (both the standalone check and the `ha_gatt_busy() || gatt_exec_busy()` guard).
4. CMake: drop `gatt_exec.c` from `SRCS`, add `ha_gatt_exec` to `REQUIRES` + `EXTRA_COMPONENT_DIRS`; delete the 3× forks.
5. Registry: `tools/gen_module_matrix.py` — `gatt_exec` `component: None` → `"ha_gatt_exec"`; regen `MATRIX.md` + `REUSE.md`;
   update `MODULES.md`. Drift-guards (`test_module_matrix`, `test_agents_nav`) must stay green.

**Radio arbitration note:** the "one central op at a time" guard (`ha_gatt_busy() || ha_gatt_exec_busy()`) stays
**caller-side in `ha_mqtt.c`**, unchanged — two separate components keep separate `s_busy`, same as the two forks do
today. A future cleanup could hoist a shared `ha_ble_central` lock, but that's out of scope here.

## 6. Risk + validation — why this is OTA-GATED
- **Live GATT-central nodes = `cbed_c6` + `coffice_c6` (c6 fleet) and `hbed_s3` (s3 fleet)** — all run the path
  (history pulls today, exec latent). A regression would break real meter history/actuation. Build-validation is
  necessary but **not sufficient** — needs a live round-trip before deploy.
- **`s3-crawlspace` is NOT in this window — it is BLOCKED** (not enrolled in `node_secrets.enc`, eFuse MAC uncaptured;
  see `edge/esp32s3-eth/nodes.yaml` TODO + memory `s3-crawlspace-hardware-swap`). Enroll it first (RESUME item #1),
  then it joins a later window. (Earlier drafts of this doc named it as the s3 target — corrected: the OTA-able s3 is `hbed_s3`.)
- **Images are branded per node** (ADR-0020 identity gate): each live node needs its OWN `enroll_node → rebuild → OTA`
  — a `.bin` built for one node is rejected by another's gate. See the §10 runbook.
- **Deploy path is a dev-signed OTA from 210, NOT a bench flash** (ops correction): c6/s3 are edge nodes; a bench
  build lacks the edge secrets, so its image fails the signed-command/OTA identity gate (`bad-sig`). So dev signs +
  OTAs from 210; ops co-runs the §6 acceptance test on the bus and stands rollback-ready.
- **Acceptance test (per flashed board):** (a) image links `libha_gatt_exec.a`; (b) a signed `{"op":"history",...}`
  command still returns `meta`/`data`/`done` on `.../reply`; (c) a signed `{"op":"gatt","steps":[{"s":"sub"...},{"s":"read"...}]}`
  round-trips `open`/`step`/`notif`/`done`; (d) a `write` step on a telemetry build still returns the
  `write disabled: telemetry-only node` error (capability boundary intact).
- c3 is **not deployed** → build-validate only (same as the c3 migration in `d46f8f9`).

## 7. Sequencing (agreed dev + ops)
1. ✅ Hugh decides §4 → **(A) Kconfig**.
2. dev builds the component + does the c3 wiring (safe, build-validated) and opens it for review.
3. Window (dev signs+OTAs from 210, ops co-runs on the bus): per-node `enroll→rebuild→OTA→§6 test`, one at a time,
   confirm green before the next — `cbed_c6` → `coffice_c6` → `hbed_s3`. Deploy gated on green; ops rollback-ready.
   (`s3-crawlspace` deferred — blocked on enroll.) See §10 for exact commands.
4. Fold the same seam pattern into the panel if/when it needs generic GATT exec (today it uses `ha_gatt` history only).

## 10. Live OTA runbook (window) — dev on 210, ops co-runs
All nodes' broker + `ota_host` = **`192.168.0.210`** (per `nodes.yaml`), so `--serve-ip` and `--broker` are both `.210`.
Fresh images already built + validated this session: `ha-edge-c6.bin` (branded **cbed_c6**), `ha-edge-s3-eth.bin`
(branded **hbed_s3**). One node at a time; do NOT proceed to the next until the §6 test is green + node stable.

```
# 0. ops: confirm on the bus + rollback-ready (watch home/edge/<node>/status + /log).
# --- cbed_c6 (current c6 image is already branded for it) ---
python3 tools/edge_ota.py --node cbed_c6 --bin edge/esp32c6/build/ha-edge-c6.bin --serve-ip 192.168.0.210 --broker 192.168.0.210
#   watch for SUCCESS (new slot + self-test PASS); run §6 (b)(c)(d) via a signed history + gatt command.
# --- coffice_c6 (re-brand + rebuild first) ---
python3 tools/enroll_node.py --node-id coffice_c6 --from-manifest --reuse --out edge/esp32c6/main/secrets.h
( cd edge/esp32c6 && idf.py build )
python3 tools/edge_ota.py --node coffice_c6 --bin edge/esp32c6/build/ha-edge-c6.bin --serve-ip 192.168.0.210 --broker 192.168.0.210
# --- hbed_s3 (current s3 image is already branded for it) ---
python3 tools/edge_ota.py --node hbed_s3 --bin edge/esp32s3-eth/build/ha-edge-s3-eth.bin --serve-ip 192.168.0.210 --broker 192.168.0.210
```
`edge_ota.py` serves the bin, sends the signed `{"op":"ota",...}`, and follows status/log to SUCCESS or ROLLBACK.
Rollback is automatic (brick-safety: a bad image fails `ha_ota_confirm_if_pending` self-test → bootloader reverts).
**Restore-the-brand note:** after the window, re-enroll c6 secrets.h back to whatever it should persist as (last was `cbed_c6`).

## 9. Review + logistics (ops, 2026-07-05)
Ops review of `@7f525f3`: **LGTM, ship it.**
- Seam analysis confirmed right (publish_reply + log sinks; `ble_scan` already-shared include-swap correct).
- **Radio arbitration stays caller-side in `ha_mqtt.c`** — ops explicitly agrees, do NOT hoist a shared `ble_central`
  lock in this change.
- Write-gate: ops concurs with **(A) Kconfig `=n`** — the only option keeping the capability boundary COMPILE-TIME.
- Name: keep `ha_gatt_exec` separate from `ha_gatt` — agreed.
- Deploy logistics: see §6 correction (dev-OTA-from-210, not bench). ops ready on ~30 min notice once §4 + the
  c3-review land; no collision with panel work (D1001 = bench, separate).

## 8. Open questions
- **§4 write-gate**: ✅ **DECIDED — (A) Kconfig** (Hugh, 2026-07-05).
- **Flash window**: when can we take a c6 + s3-crawlspace bench/flash slot? (ops to schedule.)
- **Component name**: `ha_gatt_exec` ok, or prefer folding exec+history under one `ha_gatt` with two headers? dev leans separate.
