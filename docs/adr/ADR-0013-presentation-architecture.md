# ADR-0013 — Presentation architecture (API-first, multi-client)

Status: Accepted (2026-06-22). Supersedes the earlier server-rendered/HTMX leaning.

## Context

The UI must eventually serve several very different clients:
- **browser** web app,
- **mobile** (Android/iOS — which all run browsers, so a PWA is on the table),
- **MCU-driven displays** (ESP32-class e-paper/TFT panels, e.g. Seeed D1001/E1001) that **cannot run a
  browser** — limited RAM, firmware-rendered, e-paper = seconds-slow partial refresh.

The current "dashboard" is a single inline HTML string in `api/main.py` — read-only, presentation
*coupled* to the backend. Server-rendered HTML (e.g. HTMX) was considered and **rejected**: an MCU panel
can't consume HTML, and native/mobile want JSON — so HTML coupling forces duplicate presentation paths.

## Decision

**API-first / headless.** The backend is presentation-agnostic; every client renders to its own
capability from the same data + event API.

- **Versioned JSON API (`/api/v1`)** — the single source of truth for all clients. It never returns HTML.
- **Real-time via the event bus** — browsers get an SSE (or WS↔MQTT bridge) feed; MCU panels already
  subscribe to MQTT natively. The ADR-0012 `comms_event` bus + state topics ARE this feed — one spine.
- **Per-client view-models (BFF)** for constrained displays: an e-paper panel pulls
  `/api/v1/display/<id>` = just the handful of fields it draws, on a slow cadence — not the whole model.
- **Clients:**
  - **Web + mobile = one PWA** (installable, offline via service worker, push on Android / iOS 16.4+).
    One codebase for both human-screen targets. A lean reactive layer (Preact/Svelte/Alpine + a small
    store), NOT a heavy React SPA, NOT HTMX. Vendored assets, minimal build, offline-friendly.
  - **MCU panels = firmware** consuming a BFF view-model endpoint and/or a couple of MQTT topics.
  - **Native mobile** (React Native/Flutter) only when a PWA genuinely can't do it (background BLE, the
    phone as a gateway, bulletproof background push). Deferred.
- **Auth spans client classes:** browser session (cookie/JWT) + per-device API keys for panels (the
  per-device secrets already exist — the device side is half-built).
- **Freeze the inline dashboard.** No new presentation in `main.py`; the web app becomes a static client
  of `/api/v1`, served from `.245/static` (or standalone).

## Consequences

+ One presentation-agnostic backend feeds browser, PWA, and e-paper alike; new client classes are
additive. + MQTT + the event bus give multi-client real-time for free. + PWA-first avoids writing three
apps. − A versioned API contract + an SSE/WS bridge + BFF view-model endpoints to build; the existing
inline dashboard must be migrated to a static client (small, done before it grows). Native apps and a
richer client framework are explicitly deferred until a concrete need.
