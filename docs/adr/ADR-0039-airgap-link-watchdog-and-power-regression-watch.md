# ADR-0039 — Air-gap link watchdog + power regression watch

**Status:** Accepted — built and live on .210, 2026-09-02
**Supersedes / relates to:** ADR-0032 (unregistered-device quarantine), ADR-0033 (air-gap relay),
`docs/reviews/2026-07-04-power-optimization-day9.md` (the power characterization this watches against)

---

## Context — the outage that produced this

On **2026-09-02 ~03:01–03:10 UTC** the household PWA started returning **502** from
`https://192.168.0.210`. Hugh noticed it on a wall display and asked.

`nginx` on .210 reverse-proxies the household side to ha-2 at `192.168.1.210:8123` over the WiFi air-gap
leg (`wlp2s0`). The leg had died, so nginx had no upstream.

**Why nothing caught it:** every WiFi surface read perfectly healthy.

| Signal | Reading during the outage |
|---|---|
| association | associated to `autohome_airgap` |
| signal | **-33 dBm** (excellent) |
| `beacon loss` | **0** |
| `last ack signal` | **-32 dBm** — the AP was still hardware-ACKing our frames |
| `inactive time` | 35 s (we were only hearing beacons) |
| ARP to **any** host on `192.168.1.0/24` | **FAILED**, gateway included |

The `mt7921e` held an association the AP had stopped forwarding for. The radio was up; the link carried
nothing. `sudo systemctl restart ha-airgap-bridge` — one reassociation — fixed it instantly.

**The router was ruled out by data, not assumption.** `airgap_router_pm` metered a flat **7.0 W for every
single sample** from 02:30 to 03:25 — min 7, max 7. No reboot, no brownout. That is what justifies the
watchdog reassociating *locally* rather than escalating for a router power-cycle.

Two structural gaps, both matching the standing directive that an operator dead-end means the structure is
wrong:

1. **Nothing probed the air-gap link.** Detection required a human noticing a 502.
2. **Nothing watched power.** The July campaign characterized the platform and concluded "no lever left",
   then left nothing running. "Is power okay?" was answered by a person looking at a panel.

---

## Decision 1 — `ha-airgap-linkwatch`

A oneshot tick every 30 s (`server/cluster/linkwatch.py`) that probes L3 and reassociates a dead leg.

### Probe what the fault actually breaks

Radio state is *worthless* here — it read perfect throughout. So we probe **L3 reachability**, from two
hosts by two methods:

- **gateway** `192.168.1.1` — ICMP
- **peer (ha-2)** `192.168.1.210` — ICMP **or** TCP `:8123`

Two methods so ICMP being deprioritized or rate-limited under load can't manufacture a false outage. Two
hosts because of the discrimination below.

### The discrimination that matters most

| gateway | peer | verdict | action |
|---|---|---|---|
| ok | ok | `up` | none |
| ok | **fail** | `peer_down` | **alert only — never touch the radio** |
| fail | ok | `up` | none |
| **fail** | **fail** | `down` | reassociate after hysteresis |

If the gateway answers, the link carries traffic and ha-2 is the fault. **Reassociating cannot fix a dead
peer**, and doing it anyway is exactly the shared-fate error `provisioning/required-services.yaml` already
warns about for `on_fail: failover`: a remedy must be able to fix the fault it responds to. This is why the
watchdog probes the gateway at all, rather than just checking "can I reach ha-2".

For the same reason `ha-airgap-linkwatch.timer` is **not** `on_fail: failover`. A dead air-gap leg is a
shared-fate signal — moving the VIP re-associates no radios.

### Bounded effort, then honesty

- **Hysteresis** — 3 consecutive all-down ticks (~90 s) before acting. WiFi drops packets; one lost probe
  is not an outage.
- **Cooldown** — never reassociate more than once per 120 s.
- **Budget** — after 3 failed reassociations, **SCREAM once and stop acting**. If reassociating didn't fix
  it, it isn't the zombie-association fault, and hammering the radio only obscures a human's diagnosis.
- **Flap guard** — the reassociation budget is only refilled after the link has been *stably* up for 120 s.
  Without this, a link flapping up for one tick between failures refills the budget forever.
- **Maintenance inhibit** — honors `instance/.maintenance-fit` like `service_healer.py`, so a deliberate
  deploy isn't fought. It still **alerts** while inhibited: a deploy is exactly when you want to know.

Detection-to-heal is ~90 s worst case, against ~9 minutes plus a human on 2026-09-02.

### Alerts go to the LOCAL broker

`127.0.0.1`, never the VIP. A mechanism whose entire job is reporting that the air-gap link is down cannot
depend on the air-gap link — the same rule as `failover-primitives-not-on-vip`. Kinds:
`airgap_link_down` / `airgap_link_up` / `airgap_peer_down` / `airgap_peer_up` /
`airgap_link_unrecovered` / `airgap_reassociate_failed`, all edge-triggered, plus a retained beacon on
`home/_airgap/<iface>/link` so a dashboard reads current truth and a recovery clears the banner.

---

## Decision 2 — `ha-power-watch`

A oneshot check every 30 min (`server/maintenance/power_watch.py`) comparing each metered box's rolling
window mean against a characterized baseline.

### Baselines are committed, never self-updating

`provisioning/power-baselines.yaml` holds measured values. **A baseline that re-learns itself absorbs the
exact slow regression it exists to catch** — it would ratify creep as the new normal. Re-characterizing is
a deliberate human act:

```
venv/bin/python3 -m server.maintenance.power_watch --characterize 7
```

which prints a block from real history for a person to read and commit.

### Characterize the statistic you alert on

The first cut derived `tolerance_w` from the p95 of **instantaneous** samples. That was wrong: the check
compares a **6-hour mean**, which is far smoother. Measured, it put `failover_pm`'s ceiling at roughly
**2× its baseline** — a detector that could never fire.

Tolerance is now derived from the spread of **window means** (`window_means()`): bucket history into
window-sized blocks, take each block's mean, and size tolerance to cover the worst full window observed
plus a margin. Reads as *"no window in the measured history would have alerted."*

Baselines measured 2026-09-02 over 7 days (~20.2k samples/meter, 27 × 6 h windows):

| meter | baseline | tolerance | ceiling |
|---|---|---|---|
| `plug_g11` (G11 / .210 wall) | 8.73 W | 1.9 W | 10.63 W |
| `failover_pm` | 7.15 W | 2.5 W | 9.65 W |
| `airgap_router_pm` | 7.23 W | 1.0 W | 8.23 W |

Cross-check against July: that review recorded `plug_g11` **wall** power at ~10 W against RAPL **package**
~4.8 W. Today's 8.73 W mean is *below* the reference — no regression since the campaign. (Confusing the
package figure for the wall figure would have read as a 4.8 → 8.7 W blowout. They are different quantities.)

### Absence is its own alarm

A watcher that only compares numbers it receives calls a **dead meter healthy forever**. That is precisely
ADR-0032: `airgap_router_pm` and `failover_pm` were migrated .210 → ha-2, landed unregistered, and had
telemetry silently dropped for ~23 h. So `power_meter_stale` fires when a meter stops reporting, and
`power_meter_ok` clears it.

### Where it runs, and why not both

**On ha-2 only** (`air-gap-dictator` role), where the PMs live. Deliberately **not** in `core`: the two
boxes' brokers are not bridged, so running it on both would raise the same regression twice down two
separate notification paths (.210's `ha-ntfy-bridge` and ha-2's `relay-alert-egress`). It briefly ran on
.210 during development and was moved on deployment.

### Source-agnostic reader, and "has rows" ≠ "has the window"

Local `hot.db` first, HTTP API otherwise. But the first cut preferred sqlite whenever it returned *any*
rows, and that is wrong on ha-2: **`hot.db` is pruned daily by the compactor**, so it holds roughly
today-so-far — four hours at 04:00 UTC, less right after a compaction. Non-emptiness would have silently
evaluated a 6 h window against 2 h of data, and let `--characterize` print a confident 7-day baseline
computed from an afternoon.

So the reader now checks **coverage**, not emptiness: sqlite is used only if its oldest row reaches back to
the requested start (within `COVERAGE_SLACK_S`); otherwise the API serves the range, and the source string
says so — `api(local-db short by 8.0h)`. If both are short it degrades to partial history but logs a
warning rather than reporting a clean verdict. On ha-2, `HA_POWER_API` points at `127.0.0.1:8123`
(`instance/power-watch.env`) so both paths are local and complete either way.

API reads are **paged** (`API_SLICE_S`, default 2 days). The endpoint truncates silently at 10 000 rows
with only a `truncated: true` in the body — a 7-day characterization would otherwise have been computed
from partial history and nobody would have known. A truncated slice now logs a **warning**.

Both of these are the same failure shape, and it is worth naming: **a data source that quietly returns less
than you asked for produces a confident wrong answer.** Detect the shortfall, say so, and use the source
that has the range.

---

## Alternatives rejected

| Option | Why not |
|---|---|
| Watch radio metrics (signal, beacon loss, association) | **They all read healthy during the outage.** This is the whole point. |
| Probe only ha-2 | Cannot distinguish a dead link from a dead peer — would bounce the radio for ha-2's faults. |
| Reassociate on the first failed probe | WiFi drops packets; this bounces the leg constantly. |
| Reassociate indefinitely until recovery | If 3 tries didn't fix it, it isn't our radio. Hammering obscures diagnosis. |
| Power-cycle the router on link failure | The 7.0 W flat trace proves the router wasn't at fault, and .210 cannot power-cycle it anyway. |
| `on_fail: failover` on the linkwatch timer | Shared-fate; moving the VIP re-associates no radios. |
| Auto-learned power baselines | Absorbs the regression it exists to catch. |
| Alert on instantaneous power over a threshold | Every compile and backup becomes an alert. Window mean + consecutive-check hysteresis instead. |
| Alert on a power *drop* too | Deliberately out of scope: a box being off is loudly obvious by other means, and the plausible thresholds are noisy. Staleness already covers "the meter died". |

## Consequences

- The 2026-09-02 failure mode now self-heals in ~90 s with no human.
- A fault the watchdog *cannot* fix is escalated once and then left alone, with the reason stated.
- Power drift and dead meters are continuously watched rather than noticed.
- One oneshot timer per box: `ha-airgap-linkwatch` on .210 (the leg is .210's), `ha-power-watch` on ha-2
  (the meters are ha-2's). Both `Nice`d/idle-scheduled where it matters; the linkwatch tick is two pings and
  (only when needed) one TCP connect.
- ha-2 gained a unit by scp, per the air-gap deploy protocol — `git-committed ≠ deployed-on-ha-2`. The
  manifest diff was reviewed before overwriting prod's copy: only the two new timers, nothing dropped.
- No VIP inhibit was needed: adding a new oneshot timer restarts nothing existing.

## Verification

- `tests/test_linkwatch.py` — 18 tests over the pure decision core: hysteresis, the gateway/peer
  discrimination, cooldown, escalation-then-silence, flap guard, edge-triggered alerts, missed-tick latch,
  maintenance inhibit.
- `tests/test_power_watch.py` — 26 tests: drift hysteresis, window boundaries, mean-not-max, staleness and
  its recovery, `window_means` bucketing, source selection under a short local DB, and a guard that the
  **shipped** baselines file parses with every ceiling above its baseline.
- Two live fault-injection drills of the linkwatch on .210 against a scratch state file
  (`HA_LINKWATCH_STATE`): blackhole probes drove hysteresis → reassociate → cooldown and surfaced
  `airgap_reassociate_failed`; a healthy gateway with a dead peer alerted once and never touched the radio.
- On ha-2: all five deployed files checksum-verified against .210, 26/26 tests pass **on ha-2**, one real
  systemd run, retained beacon confirmed on ha-2's bus, timer enabled.
- Supervisors after deployment: **.210 ok=41 warn=0 GAP=0**, **ha-2 ok=27 warn=0 GAP=0**.
