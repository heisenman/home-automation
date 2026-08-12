# ADR-0038 — Native mobile shell (Android)

**Date:** 2026-08-12  **Status:** **Phases 0–2 BUILT** — debug APK produced on `.210`, JVM tests green;
on-device verification pending (needs a phone). Not yet distributed.
**Owner:** dev.  **Decider:** Hugh.
**Related:** ADR-0013 (presentation, API-first + per-client BFF), ADR-0019 (screen interfaces / shared
UI spec), ADR-0017 (TLS + token auth), ADR-0033 (air-gap relay, TLS lifecycle),
`docs/decisions/air-gap-notify.md` (alerts ride MQTT).

## Context

Hugh asked for a dedicated, side-loadable phone build of the PWA. The interesting part is not packaging —
it is that a native shell **fixes something that is broken today**, and closes a consequence we accepted
in writing over a year of development.

`docs/decisions/air-gap-notify.md` (2026-06-25) established two things by live testing:

1. **Browsers refuse to register a service worker under a self-signed cert** — even after the user clicks
   through the page warning (`"An SSL certificate error occurred when fetching the script"`). Fixing that
   in a browser needs a **local CA installed per device**.
2. **Web Push routes through the vendor cloud** (Google FCM / Mozilla autopush). Both the client *and*
   the server must reach it. On an air-gapped network, neither can.

So it dropped Web Push, moved alerts to the system's own bus — `home/_alerts` (retained snapshot) and
`home/_alert/new` (edge-triggered) — and closed with a single open consequence:

> **−** No background phone notification until a LAN consumer exists.

The practical state on a phone today: the PWA has **no service worker at all**, so no offline shell, no
reliable install, and no notification when the app is closed. It is a browser page behind a warning.

**This app is the LAN consumer that decision called for.** It is the last piece of an accepted design.

## Decisions

### 1. A thin shell, not a second UI codebase

The app hosts the existing PWA in a `WebView`. We do **not** rebuild the UI natively (React Native,
Flutter, Compose). The PWA is ~2,800 lines of already-working, already-maintained code, and ADR-0013 +
ADR-0019 established the pattern: one API-first backend, per-client renderers of a shared server-authored
spec. The phone becomes the third renderer alongside the browser and the D1001 panel — not a fork.

### 2. The UI is SERVED, not bundled

The WebView loads `https://<endpoint>/app/`. The APK does not package the web assets.

This is the load-bearing choice. Bundling (the Capacitor/Cordova model) would mean **every PWA tweak
requires an APK rebuild and a re-side-load on every phone** — a regression against the current workflow
(deploy to ha-2, every client has it) and against ADR-0019's explicit "updatable without reflashing"
principle for panel clients.

It also means **`server/web/app.js` needed no changes at all**. Every PWA fetch is a relative path through
two helpers (`getJSON` / `adminSend`), so loading from the server origin resolves them for free. There is
no API-base-URL refactor in this work, and no divergence risk between "the app version" and "the web
version" — there is only one.

### 3. Trust is pinned, not clicked through

`network_security_config.xml` ships `instance/tls/server.crt` as the **only** trust anchor for
`192.168.0.210` and `192.168.0.200`. `system` is deliberately excluded from that domain config: for these
hosts our cert is the only acceptable one, and a different cert must fail closed. `onReceivedSslError`
**never** calls `proceed()` — clicking through would reduce the app to the browser behaviour it exists to
escape.

Consequences: no per-device CA install, no interstitial, a genuine secure context (so `crypto.subtle`
takes its fast path for the admin token), and — as a bonus we do not depend on — the service worker
should now be able to register.

The cost is a **frozen copy of a cert that lives outside the build**. `tests/test_android_cert_pin.py`
guards it: bundled copy must match `instance/tls/server.crt`, must not be within 90 days of expiry (the
2028-04-05 date ADR-0033 flags as an air-gap time-bomb), and every pinned host must appear in the cert's
SAN list.

### 4. Alerts ride the existing MQTT bus

A foreground service subscribes to the same two topics the wall panels read. **Verified during design**
that a phone on the household LAN can reach them — the retained snapshot arrives on connect:

```
$ mosquitto_sub -h 192.168.0.210 -t 'home/_alerts'
home/_alerts {"schema": 1, "ts": 1786514411.36, "alerts": []}
```

No bridge, no cloud, no APNs/FCM. Severity maps to notification **channels** (`critical` / `warning` /
`info`) rather than a priority int, because a channel is the thing the household can actually govern —
letting critical break Do Not Disturb and silencing notices — from system settings, without us shipping a
preferences screen for it.

The retained snapshot is used for **withdrawal**, not notification: re-announcing every standing alert on
each reconnect would be noise, but clearing notifications for alerts that have since resolved keeps the
shade a picture of the house rather than a history of it.

### 5. `specialUse`, not `dataSync`

The foreground service type is `specialUse` with a declared subtype. On Android 15, `dataSync` is capped
at ~6 cumulative hours per day before the system stops it, and may not be started from `BOOT_COMPLETED`.
Either restriction alone turns an always-on alert lane into one that **goes quiet without saying so** —
the single failure mode an alerting system may not have. `specialUse` is also simply the honest
description: this use case is not one of the enumerated types.

## Scope

**In:** Android. **Out, deliberately:**

- **iOS.** Needs a signing identity; Hugh's call was to skip the $99/yr Apple Developer Program for now.
  The shell concept ports (SwiftUI + `WKWebView`, same pinned trust), but iOS suspends background sockets,
  so an iOS build gets **foreground-only alerts** unless we adopt APNs — which would reopen exactly the
  vendor-cloud dependency this design and ADR-0033 cut. Revisit as its own decision, not a footnote.
- **Off-LAN access.** Home Wi-Fi only. Remote access would need a VPN on `.210`, and `.210` is the
  dual-homed crown jewel — that is an ADR-0033-scoped effort with its own threat model, not a setting.

## Consequences

- **+** Closes the open consequence in `air-gap-notify.md`: background phone alerts, zero cloud.
- **+** One UI codebase still. A PWA deploy reaches the phone with no APK rebuild.
- **+** The cert stops being friction and becomes a security property.
- **−** The pin must be maintained across cert rotation; guarded by test, but it is a real coupling.
- **−** Android battery management may still throttle a long-lived socket on some OEM skins. Mitigated by
  the foreground service and network-callback reconnect; an exemption prompt is available if needed.
- **−** Side-loading means no automatic updates. Phase 3 hosts the APK with a version manifest so the app
  can at least *say* an update exists.

## Security once-over

Per the standing per-feature review:

- **The app only subscribes** to MQTT; it publishes nothing. It adds no new authority to the bus.
- **The broker's household listener is `allow_anonymous true`** (`/etc/mosquitto/conf.d/`). Anyone on the
  home Wi-Fi can already subscribe *and publish* — pre-existing, and consistent with the accepted
  "auth posture by network context" (auth required when internet-connected, optional on a trusted LAN).
  Named here so it is inherited knowingly rather than silently. If the posture tightens, the app needs
  credentials, and that is a one-line change to the connect builder.
- **No secret is bundled.** Build-artifact scan of the APK: no private key material (the only PEM strings
  are Netty's parser literals), no WiFi credentials, no air-gap addresses, and the master/root passphrases
  do not appear. The only credential-adjacent file shipped is the **public** server certificate.
- **The admin bearer stays in the WebView's DOM storage**, where the PWA already puts it — a derived
  SHA-256, never the passphrase. Inside an app that storage is private to this UID rather than shared with
  every tab of a browser profile, so it is strictly better isolated than today, and wrapping it in a
  second store would add moving parts without adding protection.
- **`allow_origins=["*"]` on the API** is unchanged and unaffected: the app is same-origin with the page
  it loads, and auth is a bearer header, not a cookie, so CORS is not the control here.
