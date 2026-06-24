# PWA Web Push — payload-less tickle (board: pwa-web-push)

**Status:** BUILT + unit-tested (offline). End-to-end delivery is **gated on HTTPS** (see Constraint). · 2026-06-24

## Design
Browser subscribes → server stores the subscription → when a **new** alert appears, the server sends an
**empty, VAPID-signed push** ("tickle"); the service worker wakes, fetches `/api/v1/alerts`, and shows the
most-severe notification. We carry **no payload**, so we sign only the VAPID JWT (RFC 8292, ES256) with the
**existing `cryptography`** dep — no `pywebpush`, no RFC-8291 payload encryption.

- `tools/gen_vapid.py` — P-256 VAPID keypair → `instance/vapid.json` (**gitignored secret**, mode 600).
- `server/api/webpush.py` — `vapid_auth_header()` (JWT, ES256) + `send_tickle()` (empty POST) + `alert_key()`.
- `server/control/control_store.py` — `push_subscription` table (in control.db → rides sync-standby, so subs
  survive failover) + `add/remove/all_push_subs`.
- `server/api/main.py` — `GET /api/v1/push/vapid-public-key`, `POST /api/v1/push/{subscribe,unsubscribe}`,
  and a **VIP-gated** background loop: every `HA_PUSH_POLL_S` (60s) the dictator diffs active alerts and
  tickles all subs on a newly-appearing alert (prunes 404/410). Standby never pushes (one-controller invariant).
- `server/web/sw.js` — `push` (fetch alerts → showNotification) + `notificationclick` (focus/open) handlers; cache → v22.
- `server/web/push.js` + `app.js` — a `🔔 On/Off` toggle in the topbar (subscribe/unsubscribe).

## ⚠️ Constraint — needs a secure context (HTTPS)
ServiceWorker + PushManager require a **secure context**: HTTPS, or `localhost`. Over the plain-http LAN
(`http://192.168.0.200/app`) `pushManager.subscribe()` will fail — so **end-to-end push delivery is coupled to
the TLS work (`tls-r9-auth`, R9).** The code is complete and correct; it goes live the moment the PWA is served
over HTTPS (the toggle simply returns `unsupported` until then, no errors). Unit tests prove the crypto/store now.

## Deploy (gated — restarts ha-api on the dictator)
1. Generate keys ON the box (writes the gitignored secret): `python3 tools/gen_vapid.py` (subject defaults to Hugh's email).
2. `git pull` to this commit; restart `ha-api` (picks up routes + the push loop). Bumped SW cache (v22) self-updates clients.
3. Once served over HTTPS: open `/app`, tap **🔔 Off → On**, grant permission. Trigger a test alert (e.g. let a
   meter go stale > threshold) and confirm a background notification arrives + click focuses the app.

## Tests
`tests/test_webpush.py` — VAPID JWT **verifies against the public key** + per-endpoint `aud` + `gen_vapid`
roundtrip + subscription-store add/idempotent-upsert/remove. 21/21 pure tests green on desktop.
