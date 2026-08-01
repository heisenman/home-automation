# RESUME — 2026-08-01 · PWA-driven firmware flashing (+ the power/failover fix)

Pick-up doc for the session that shipped **flash-a-blank-board-from-the-browser** end to end, on top of
node-born secrets. Everything below is on `origin/main` and deployed to ha-2 unless stated.

## Where it stands

**Three real ESP32-C6 boards were provisioned entirely from the prod PWA** and appear on ha-2:

| node | MAC | state |
|---|---|---|
| `sgp40_stdby1` | `A0:F2:62:86:F5:74` | flashed `profile=prod`, unpowered |
| `sgp40_stdby2` | `10:BD:A3:A1:6E:3C` | flashed `profile=prod`, unpowered |
| `sgp40_stdby3` | `10:BD:A3:A0:96:BC` | flashed `profile=prod`, **online** |

All `v25-generic`, all `enrolled:false` (each holds a secret it minted itself that nobody else knows),
all advertising `sgp40_gas` from **autodetection**, not configuration.

**NEXT STEP (Hugh, tomorrow):** intake — adopt one into its real house location once the right cable
arrives. The adopt path is already proven; nothing new needs building for it.

## The arc, and where each piece lives

```
blank board ──flash from PWA──> node ──self-mints secret──> announces hello
                                                                  │
                                            adopt in PWA ─────────┘
                                    (TOFU claim + auto-register + relocate)
```

| Piece | Commit | Where |
|---|---|---|
| Node-born secret + TOFU claim (ADR-0036 L0/L3) | `b3d7c00` | `firmware/components/ha_config`, `ha_mqtt`, `server/control/edge_enroll.py` |
| Intake fixes (auto-register new hw, claim-after-validate, dormant birth) | `fbe9f26` | `server/api/control.py` |
| Phase 0 — de-brand the image | `412da0a` | `ha_gas` autodetect, `ha_config` NVS + eFuse bind, `ha_ota` generic gate |
| Phase 1 — flash driver | `edf39b7` | `server/maintenance/edge_flash.py` |
| Phases 2+3 — PWA panel, job, image manifest | `d7348af` | `+ admin_job` flash op, `server/web/app.js` |
| Network profiles | `f1dfe41` | `instance/edge-provision.yaml` (gitignored) |

## Things that will bite whoever picks this up

* **Broker and Wi-Fi are ONE choice.** The first prod flash failed because the UI offered a bare broker
  dropdown against a single site SSID: the board joined `CTWap_24g`, pointed at `192.168.1.200`, came up
  perfectly healthy and could never connect. Profiles now bundle ssid+psk+broker+ota, and a mismatched
  pair is refused. Don't reintroduce a free-form broker field.
* **`autohome_airgap` is hidden on both bands** but exists on 2.4 GHz (the C6 is 2.4-only). The prod
  profile carries that warning. In practice the C6s joined it hidden — the older
  "must un-hide for intake" note may be more conservative than needed for these chips.
* **`Path.home()` lies inside these units.** `ha-api` and `ha-admin-job` both set
  `Environment=HOME=<repo>/instance`, so toolchain paths built from it resolve under `instance/`. Use
  `pwd.getpwuid(os.getuid()).pw_dir`.
* **`cp -a` defeats the IDF rebuild** — it preserves mtime, ninja skips the rebuild, and the `.bin` keeps
  the *previous* node's identity. `touch` after restoring, then grep the `.bin` for the node id.
* **A local row delete doesn't stick** — the peer still has it and the next reconcile merges it back.
  Verify destructive cleanup *after* a reconcile cycle, not immediately.
* **Re-flashing an enrolled node orphans it** — new node-born secret, stale LUT entry, TOFU-lock refuses
  the re-claim. The flasher detects this and requires `confirm_rotate` (Hugh's policy: auto-rotate is
  fine because holding the cable is the trust root, but never silently).

## Not done

* **Generic images for `esp32s3` / `esp32c3`.** Both still carry per-node builds, so the manifest reports
  them not-ready *with the reason*. Only the C6 is flashable today.
* **Firmware tree not synced to ha-2.** ha-2 is the C6 OTA origin (`c6-fleet-ota-from-ha2`), so the
  Phase-0 `firmware/` + `edge/` changes should land there before the next OTA from prod.
* **The browser UI was never exercised by me** — Hugh drove every PWA interaction. Two of the three bugs
  found this session came out of his runs, not my tests.

## Also shipped this session (unrelated to flashing)

A power review turned into a **live failover outage fix** (`c73edca`): keepalived's healthcheck probed
`/api/v1/sensors` (an unbounded `GROUP BY`), which grew past its 4 s timeout, pinned a core (+8 W) and
**silently disarmed the air-gap failover for 4 days**. Probe is now `/health`; the standby got the
compactor its unit set was missing. Both boxes dropped below their pre-incident baselines. See
`liveness-probe-must-be-o1` in memory.
