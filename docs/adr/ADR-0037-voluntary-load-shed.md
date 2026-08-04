# ADR-0037 — Voluntary grid load-shed

**Date:** 2026-08-04  **Status:** **ARMED on .210 with `source=nws`** (2026-08-04), timer live.
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

## Amendment — armed, and the timing bug the live feed exposed (2026-08-04)

Armed with `source=nws` because a real Heat Advisory was in the feed (Aug 4 12:00 → Aug 5 23:00 PDT),
giving a genuine end-to-end test before the travel window. `imap` remains the higher-fidelity target.

Verifying against the live feed immediately caught a bug that unit tests on invented data would not have:
**`/alerts/active` lists an alert the moment it is ISSUED, not when it takes effect.** At 20:30 PDT on
Aug 3 the feed already carried the Heat Advisory whose onset was Aug 4 12:00 — and 20:30 is inside the
17:00–21:00 peak window, so the lane would have curtailed the house that evening for a heat wave that had
not started. A qualifying alert must now satisfy `onset <= now < ends`, and the shed window is **clipped
to the alert's own end** so a shed cannot outlive the condition that justified it.

Also live that day: three **Air Quality Alerts** with "Areas Of Smoke" in the forecast at 94–95 °F. That
is the per-device PM2.5 guardrail's exact scenario, and it is why an Air Quality Alert is deliberately
**not** in the qualifying-event list — smoke is not a grid event, and it is answered better by the
purifier's own measured indoor PM2.5 than by an area-wide advisory.

## Open, and deliberately not guessed

- **`imap` parsing** waits on a real forwarded PGE notification — the only source that is the actual PGE
  signal, with true start/stop times, rather than a proxy.
- **The purifier's control policy has a dangling `source_sensor`** (`levoit_office`, which has *zero*
  rows ever recorded) while its PM2.5 actually arrives under `purifier_living_room` (1.0 µg/m³, fresh,
  authoritative). Its automation is `enabled: false`. Until that is repointed the purifier cannot be
  guardrailed and so will never shed. Not fixed here: repointing a control policy changes prod automation
  behaviour for a device that is deliberately disabled, which is Hugh's call, not a drive-by.
  **Note this is worse than it first looks:** with `enabled: false` the controller skips the device
  entirely, so an override written to the purifier is never even read. Until BOTH the source_sensor is
  repointed and automation is enabled, the purifier cannot participate in a shed at all — the lane will
  faithfully log HOLD for it forever.
