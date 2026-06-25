# Decision — alerts ride MQTT (air-gap-native), drop vendor Web Push

**Date:** 2026-06-25 · **Owner:** ops · **Decider:** Hugh
**Supersedes the delivery half of:** `pwa-web-push` (see [pwa-web-push.md](pwa-web-push.md)).

## Context
We unblocked the browser secure-context (R9 HTTPS:8443) to finish the built-but-dark PWA Web Push 🔔.
Live testing exposed two blockers that are fatal **for the air-gapped end state**, not just bugs:

1. **Service workers need a genuinely trusted cert.** Browsers refuse to register a SW from a self-signed
   cert *even after the user clicks through the page warning* (`"An SSL certificate error occurred when
   fetching the script"`). So `/app/sw.js` never registers → no push subscription. Fixing this needs a
   **local CA** installed per device (not just a click-through exception).
2. **Web Push routes through the vendor cloud.** The browser subscribes against, and the server delivers
   via, **Google FCM / Mozilla autopush**. Both the client *and* the server must reach that cloud. On the
   planned **air-gapped** network neither can — so Web Push cannot work there *even with* a local CA.

Net: making Web Push work = build a local CA **and** depend on a cloud we are about to cut off. Low value.

## Decision
- **Drop vendor Web Push** as the notification path. Keep the **in-app alert banner** (the PWA already
  polls `/api/v1/alerts` every 5s while open — works today, no cert/CA fuss).
- **Propagate system events on the system's own bus: MQTT** — the air-gap-native choice (everything else
  already speaks it; no cloud). The single alert engine (`_build_current_alerts` → `viewmodel.build_alerts`)
  stays the one source of alert rules; the API's background loop now publishes it on the dictator only:
  - **`home/_alerts`** — RETAINED snapshot of the active alert set (`{schema,ts,alerts[]}`). A panel or
    display that connects at any time immediately gets current state.
  - **`home/_alert/new`** — non-retained, one publish per newly-appeared alert. Edge-triggered, for a
    notifier to turn into a phone/desk alert.
- `crypto.subtle` (the real R9 reason for HTTPS) **works on the click-through cert** — that benefit stands;
  no local CA required for it.

## Consequences
- **+** Alerts now propagate with zero cloud dependency — survives the air gap. **+** Reuses the existing
  alert engine + broker; the controller already publishes events to MQTT, so consumers are a known pattern.
  **+** Retained snapshot + edge event covers both "display current state" and "notify on change."
  **−** No background phone notification until a LAN consumer exists (below). The in-app banner covers
  foreground.
- The deployed `ha-api-tls` push loop is now the MQTT publisher; the web-push tickle is a **deprecated
  no-op** (0 subscriptions, kept until the PWA 🔔 is fully removed). `push_subscription` table + VAPID keys
  stay harmless; remove in the cleanup slice.

## Consumer options (next slice — pick the LAN delivery endpoints)
All LAN-only, no vendor cloud:
- **Self-hosted ntfy** (recommended for phones): run `ntfy` on the LAN; an MQTT→ntfy bridge turns
  `home/_alert/new` into a phone notification. The ntfy Android app supports a self-hosted server over the
  local network — genuine air-gap push to a phone.
- **MCU / wall panel** (dev's domain): an ESP32 panel subscribes to `home/_alerts` and shows/buzzes —
  the codebase already anticipates "MCU panels" as an alert consumer.
- **Desk/host notifier**: a small service on a LAN host subscribing to `home/_alert/new`.

## Status
- **Done:** MQTT alert propagation in the API loop (`home/_alerts` retained + `home/_alert/new`),
  VIP-gated, broker-reconnecting. Deploy = restart `ha-api-tls` (additive; never touches `:8123`).
- **Next:** choose consumer endpoint(s) above and build the bridge/panel (board `air-gap-notify`).
- **Cleanup (later):** retire the PWA 🔔 + `push_subscription`/VAPID once a consumer is live.
