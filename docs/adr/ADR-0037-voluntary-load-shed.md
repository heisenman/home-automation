# ADR-0037 — Voluntary grid load-shed

**Date:** 2026-08-04  **Status:** Built, installed **disarmed** (no trigger source chosen yet).
**Owner:** dev.  **Related:** ADR-0011 (automation controller + manual override), ADR-0013 (BFF),
ADR-0033 (air-gap relay).

## Context

A heat wave, and a household about to be empty for ~1.5 weeks. The ask: notice when Portland General
Electric calls for load reduction and voluntarily stop the dehumidifier and the air purifier for the
event, without anyone at home to do it.

The actuation half already exists and is proven: an arbitrary-duration manual override with setpoint
parking (ADR-0011 addendum, 2026-08-02) is exactly a voluntary-curtailment primitive. What was missing is
a signal and a safe way to act on it unattended.

### The finding that shapes everything: there is nothing to poll

**PGE publishes no machine-readable load-warning signal.**

- Peak Time Events are delivered to **enrolled customers by email/SMS only** — one notice the day before,
  one the day of, carrying the start and stop times.
- `portlandgeneral.com/peak-time-events` is marketing copy: no event status, no dates, no countdown, no
  JSON, no embedded data. A scraper learns nothing.
- The unofficial `portlandgeneral-api` library talks to their GraphQL endpoint but requires **customer
  account credentials** and exposes billing/usage — nothing about events, demand response, or grid state.
- Summer season is Jun 1 – Sep 30; events are typically 17:00–21:00.

So a cron that "checks PGE" has nothing to check. Pretending otherwise — scraping a page that does not
carry the data, or inferring an event from a proxy and calling it a PGE event — would produce a lane that
looks authoritative and is not.

## Decision

Build the **mechanism** now with the **trigger as a pluggable choice**, the same shape as
`server/weather/sources.py` and the "pluggable inputs" stance already taken in `mode.py`. A source
answers one question — *is there a curtailment window now, and when does it end?* — and everything
downstream is source-agnostic.

**Host: .210, structurally.** It is the only box that sees both the public internet (the signal) and the
air-gapped control plane on 192.168.1.x (ha-2 holds the VIP and drives the actuators). ha-2 has no
internet route, so this is not a convenience placement. It commands ha-2 through the **same admin control
API a human uses from the PWA**, with a bearer derived locally from the master passphrase — no privileged
side channel, and nothing the lane can do that a person could not do from the app.

**Sources implemented:** `schedule` (fixed window over a season — no signal, no credentials, no guessing)
and `nws` (api.weather.gov active heat alerts for a point, keyless — an explicit *proxy*, which will fire
on days PGE called nothing and miss any non-heat event).

**Sources declared but deliberately NOT implemented:** `imap` (ingest PGE's own mail — the highest
fidelity option, since it carries the real start/stop times — but parsing it means guessing at a message
format nobody here has seen, and a parser written against an imagined email is fabrication) and `manual`
(a remote trigger, pending a decision on PWA control vs an ntfy hook). Both **fail loudly** at startup
rather than silently doing nothing.

## The three properties the decision law is built around

1. **A human always wins.** A device already carrying an override this lane did not set is not touched —
   not to shed, not to release. ADR-0011 puts a manual override above ambient policy, and an automatic
   curtailment is ambient by definition. Ownership is matched on expiry, because the override row has
   nowhere to carry a marker (the API stores `(action, expiry)` and nothing else).

2. **An unverifiable guardrail forbids the shed.** No reading, or a stale one, means we cannot show the
   house is safe to curtail — so we do not shed, and we release one in progress. Failing toward comfort
   is deliberate: the cost of not shedding is money; the cost of wrongly shedding is a humid house, or an
   unfiltered one, that nobody is home to notice.

3. **The override outlives us, but not the window.** Every shed is a TTL sized to end exactly at the
   window end, re-asserted each run. If the lane dies mid-event — box down, network gone, crash loop —
   the shed still expires on time by itself. **Nothing here can strand the house.** This is the single
   reason the whole design is safe to leave running while nobody is home.

## The guardrail is per-device, in that device's own control metric

Hugh's guardrail ask was "an RH ceiling vetoes the shed", framed around the dehumidifier. Applying that
one number to both devices is wrong in a way that hides:

- The purifier's control metric is **PM2.5**, not humidity. It reports **no** humidity at all, so an RH
  ceiling on it is *permanently unverifiable* — under rule 2 it would **silently never shed**, while
  every log line and every surface looked healthy. Half the requested feature would simply not happen.
- And the guardrail that actually matters for a purifier during a Pacific Northwest heat wave is
  **smoke**: heat waves are wildfire season here. Shutting the purifier off into smoke to save a few
  cents is the wrong trade, and RH cannot express it.

So the guardrail reads **each device's own control metric from its own authoritative control sensor** —
the very number its automation runs on. `%RH ≥ 55` for the dehumidifier (its rule runs 44/40, so 55 is
well clear of normal); `PM2.5 ≥ 35 µg/m³` for the purifier (its speed bands are 12/35/55, and 35 is the
US AQI "unhealthy for sensitive groups" line). Units differ per device and are not comparable, which is
exactly why there is no global ceiling and why **every device must have an explicit ceiling or the lane
refuses to start** — an unlisted device is a config error caught at startup, not a surprise at 5pm.
`<device>:none` opts out deliberately.

Not the appliance's onboard sensor either: the Midea's hygrometer reads its own dried return air (~14
points low while running), so it would happily certify a house that is not actually dry.

## Consequences

+ The mechanism is done and testable without a network; the trigger becomes a config line.
+ Every failure path ends in "do not shed" — no source, no signal, no network, no reading, bad config.
  The worst thing this lane can do on a bad day is nothing.
+ Reuses the proven override + setpoint-park primitive, so the dehumidifier genuinely stops rather than
  idling against its old target.
− A dev-box dependency: if .210 is down there is no shedding. Fail-safe (devices run normally), but it
  means the lane is not part of prod's self-sufficiency story (see `prod-self-sufficient-is-the-goal`).
− `schedule` and `nws` are approximations of a PGE event, and are labelled as such in the logs and config
  rather than dressed up as the real signal.

## Open, and deliberately not guessed

- **Which trigger.** Unanswered. The lane is installed **disarmed** — `HA_GRID_SOURCE` unset means it
  refuses to run — so the units can exist before the question is settled.
- **`imap` parsing** waits on a real forwarded PGE notification.
- **The purifier's control policy has a dangling `source_sensor`** (`levoit_office`, which has *zero*
  rows ever recorded) while its PM2.5 actually arrives under `purifier_living_room` (1.0 µg/m³, fresh,
  authoritative). Its automation is `enabled: false`. Until that is repointed the purifier cannot be
  guardrailed and so will never shed. Not fixed here: repointing a control policy changes prod automation
  behaviour for a device that is deliberately disabled, which is Hugh's call, not a drive-by.
