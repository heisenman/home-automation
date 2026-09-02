# ADR-0040 — Recording watch: is every device actually still recording?

**Status:** Accepted — built and live on ha-2, 2026-09-02
**Relates to:** ADR-0032 (unregistered-device quarantine), ADR-0039 (link watchdog + power regression watch),
`tools/gap_watcher.py`

---

## Context — two sensors were dark for two days and nothing said so

A brownout on **2026-08-31 ~04:16 UTC** cold-booted the edge fleet. Two nodes never rejoined the
then-hidden SSID (root cause and fix: ADR-0039 / `docs/airgap/ROUTER-CONTROL.md`), so **`gas_hbed`** and
**`gas_kitchen`** stopped recording. They stayed dark for **two days**. Hugh found it by looking at the PWA
map and asking "have I lost some gas sensors?"

There *is* a watcher for exactly this. `ha-gap-watcher.timer` runs daily. Its output across the outage:

```
2026-08-30 ... gap watcher: 0 device(s) with gaps to backfill
2026-08-31 ... gap watcher: 0 device(s) with gaps to backfill
2026-09-01 ... gap watcher: 0 device(s) with gaps to backfill
2026-09-02 ... gap watcher: 0 device(s) with gaps to backfill
```

Four clean runs, straight through. It is not broken — it is **structurally incapable** of seeing this:

```python
def find_gaps(times_sorted, min_gap_s):
    return [(a, b, b - a) for a, b in zip(times_sorted, times_sorted[1:]) if b - a > min_gap_s]
```

It pairs **consecutive readings**. A device that stops reporting produces no later reading, so the trailing
silence yields no pair and therefore no gap. Interior gaps are found; **a device that flatlines and stays
dead is invisible.** Once the death passes the lookback window (`3d`) the row set is empty and the device is
`continue`d entirely — doubly silent.

It is also, by design, a **backfill router** — it finds recoverable history and dispatches a pull. Noticing
loss is a different job from recovering it, and it was never doing the first one.

This is the third instance of one failure shape in as many days:

| | the blind spot |
|---|---|
| ADR-0032 | migrated meters landed unregistered; telemetry silently dropped ~23 h |
| ADR-0039 (`power_watch`) | a watcher that only compares numbers it *receives* calls a dead meter healthy forever |
| ADR-0039 (`linkwatch`) | radio metrics all read healthy while the link carried nothing |
| **here** | a gap-finder that only inspects *arrived* readings cannot see a device that stopped |

**The rule: a check that only inspects what arrived cannot notice absence. Absence has to be asked about by
name.**

## Decision

`ha-recording-watch` (`server/maintenance/recording_watch.py`) inverts the question. It does not ask *"are
the readings I received well spaced?"* — it asks *"is everything that **should** be reporting, reporting?"*,
driven by the **roster**, not the data.

### Source: `device_last_seen`

`device_id → (device_type, area, last_ts)`. Exactly right here, for one property: it **retains a dead
device's last timestamp** rather than losing it when the readings are pruned. Verified during the incident —
it still held `gas_kitchen` at 62.1 h stale and `e1001_c_office` at 885 h while both were absent from
everything else. The registry is then unioned in to catch devices that have **never** reported, which
`device_last_seen` cannot know about by construction.

### Four states, and only one of them alerts

| state | meaning | alerts? |
|---|---|---|
| `ok` | within its staleness threshold | no |
| `late` | quiet past threshold, under the dormant cutoff | **yes — critical** |
| `dormant` | quiet > 7 d — long-retired hardware | no, digest only |
| `never` | registered, never reported | no, digest only |

`dormant` exists so day-one noise doesn't bury the signal. On first run `e1001_c_office` had been silent
**885 h** (a battery e-ink *display*, not a sensor — losing it costs no data). Alerting on that alongside
the two sensors that actually mattered is how a monitor becomes decoration.

### Thresholds are deliberately loose

Default **60 min**, against observed cadences of ~10 s (gas), ~10–60 s (BLE meters), ~30 s (Tasmota). This
detector's job is catching a device that **stopped**, not policing jitter. A tight threshold buys minutes of
detection speed and pays in false alarms that train the reader to ignore it. 60 min still turns a two-day
outage into a ~1.5 h one.

### Twice-daily digest — the part that is not obvious

Alerts are edge-triggered, so a healthy system is silent. But **silence is exactly what failed here.** A
monitor that only speaks on failure is indistinguishable from a monitor that has itself died. So a roster
digest is published at `digest_hours_utc: [8, 20]` **even when everything is fine**:

```
✓ all 24 active devices recording (1 inactive, listed below) — 24/25 recording |
  dormant (>7d, not alerted): e1001_c_office
```

The wording is precise on purpose: an early cut said "all devices recording" while one was dormant, which
is the kind of small overclaim that teaches a reader to discount the message. A missed digest slot fires
**late rather than being skipped**, for the same reason.

## Alternatives rejected

| Option | Why not |
|---|---|
| Fix `gap_watcher` to also check trailing silence | It is a backfill router with its own routing/dispatch concerns, and it runs on .210 against .210's registry+DB. Liveness belongs where the roster and data of record live (ha-2), on a much faster tick than daily. Left alone; the two are complementary. |
| Alert from the readings stream (a watchdog timer per device) | Requires holding per-device timers in a live process and re-arming across restarts. A stateless roster sweep over `device_last_seen` is simpler and crash-safe. |
| Derive each threshold from observed cadence automatically | Same trap as auto-learned power baselines (ADR-0039): a device slowly degrading to a longer interval would have its own decay ratified as normal. Thresholds are config. |
| Alert on `dormant` / `never` too | Buries the real signal under retired hardware on day one. They appear in the digest, where a human can retire them. |
| Put it in the `core` role | The two boxes' brokers are not bridged, so it would raise every alert twice down two notification paths — same reasoning as `ha-power-watch`. ha-2 only (`air-gap-dictator`). |

## Consequences

- A device that stops recording is announced within ~1.5 h instead of never.
- A positive "system is fine" signal arrives twice a day, so the monitor's own liveness is observable.
- `dormant`/`never` give a standing, low-noise inventory of things worth retiring — `e1001_c_office` is the
  first entry.
- One more oneshot timer on ha-2 (`Nice`d, idle-scheduled). Each tick is one indexed sqlite read plus a YAML
  parse.
- **`gap_watcher` is unchanged and still useful** — it recovers recoverable history. It is simply not a
  liveness monitor and should never again be mistaken for one.

## Verification

- `tests/test_recording_watch.py` — 21 tests, including
  `test_the_2026_08_31_outage_would_now_be_caught`, which replays the exact roster (two sensors at 62 h,
  rest healthy) and asserts precisely those two are flagged.
- Live on ha-2 against the real fleet: `roster 25: ok=24 late=0 dormant=1 never=0`, `e1001_c_office`
  correctly `DORMANT` at 885.5 h, digest published, retained beacon on `home/_recording/status`.
- Supervisor after deployment: **ha-2 ok=28 warn=0 GAP=0**.
