# ADR-0017 — API TLS + token auth (roles, expiry, rotation) [R9]

**Date:** 2026-06-25
**Status:** Proposed — board `tls-r9-auth` (owner ops). Unblocks `pwa-web-push` delivery. Builds on
[ADR-0010](ADR-0010-command-control-protocol.md)/[ADR-0011](ADR-0011-modular-infrastructure.md) (the
master-derived admin bearer) and the `secret_store` auth tokens.

## Context
Today the HTTP API/PWA auth is a **single static bearer** = `SHA256("ha-api:" + master)` (`secret_store.api_token`),
sent as `Authorization: Bearer …`, checked by `make_api_token_verifier` on every control route. It works but:
- **No expiry, no roles, no rotation** — one shared secret; a leaked token is valid until the master changes.
- **Plain HTTP on the LAN.** This blocks every browser **secure-context** feature: `crypto.subtle` is undefined
  (the PWA already ships a pure-JS SHA-256 fallback *because* of this), and **ServiceWorker/PushManager need
  HTTPS** → `pwa-web-push` is built but the 🔔 toggle is dark. TLS is the concrete unlock.
- Pre-TLS, a sniffed bearer is replayable (the confirm-token second factor still gates sensitive actions,
  but read/override aren't protected on the wire).

Out of scope (stays separate): **broker/MQTT auth** — that's `broker-auth-posture`, gated, rides the OpenWRT
air-gap cutover (ROADMAP theme B/D). This ADR is the **HTTP** plane only.

## Decision
**Add TLS + a JWT token layer with roles/expiry/rotation, backward-compatible during transition.**

1. **TLS (air-gap-friendly).** Serve `ha-api` over HTTPS with a **locally-generated** cert (no external CA /
   Let's Encrypt — there's no internet). `tools/gen_tls.py` (cryptography, mirrors `gen_vapid.py`) emits a
   key + cert with **SAN = VIP `192.168.0.200` + host + `.210`**, written to `instance/tls/` (gitignored,
   0600). uvicorn serves it (`--ssl-keyfile/--ssl-certfile`). Transition: HTTPS on **:8443** alongside the
   current HTTP **:8123** (then flip + redirect once clients move) so nothing breaks mid-rollout.
   - **Cert trust — DECISION FOR HUGH:** (a) **self-signed** + a one-time per-device "trust" (simplest,
     air-gap-clean, browser warning until accepted), or (b) a **local CA** whose root is installed once per
     device (no warnings, slightly more setup). Either yields a secure context → web-push works. *Recommend
     (a) now* (few devices, zero infra), revisit (b) at the OpenWRT cutover if warnings annoy.
2. **JWT tokens.** `POST /auth/login {password}` → if it derives the master (same `SHA256("ha-api:"+master)`
   check, so the master never crosses the wire) → issue a compact **HS256 JWT** `{sub, role, iat, exp, jti}`
   signed with a **server signing key** (separate from the master, in `instance/auth_key`, rotatable). Short
   `exp` (default **12 h**) + a `POST /auth/refresh`. The verifier accepts **either** a valid JWT **or** the
   legacy static bearer during transition (deprecate legacy after the PWA moves).
3. **Roles.** `admin` (full, incl. config/secrets/sensitive), `operator` (commands + overrides, no config),
   `viewer` (read-only). Routes gate on the minimum role. Today everything is admin; `viewer` immediately
   enables household read-only dashboards. Role is a JWT claim; legacy bearer = `admin` (back-compat).
4. **Rotation.** Rotating `instance/auth_key` invalidates all live JWTs (cheap, stateless). Optional per-token
   revocation via a `jti` denylist — **deferred** (not needed for a single-admin home system yet).

## Ops / dev split (the coordination contract)
- **ops (this work):** `gen_tls.py`; HTTPS serving (uvicorn + systemd); `server/api/auth_tokens.py` (mint/
  verify/roles/rotation, pure + unit-tested); `/auth/login` + `/auth/refresh`; role-gate the routers; update
  the PWA login to call `/auth/login` and (over HTTPS) use `crypto.subtle`; flip web-push on.
- **dev (poll for ops completion):** **likely NO firmware change** — edge nodes talk **MQTT**, not the HTTP
  API, and **OTA stays plain-HTTP + signed-hash** (ADR-0005), so HTTPS-on-API doesn't touch the edge. dev's
  task = **confirm** no edge/tool HTTP client hits `:8123` expecting plain HTTP (grep the forks + tools), and
  if any does, move it to `:8443` + trust the cert. Broker-auth stays dev/ops-joint but **separate + gated**.

## Consequences
- **+** Unblocks `pwa-web-push` + restores `crypto.subtle` (drop the JS SHA fallback). **+** Expiring,
  role-scoped, rotatable tokens; sniffed traffic is encrypted. **+** Backward-compatible rollout (dual-port +
  dual-verify) — no flag-day. **+** Stateless JWT = no session store; rotation is one key swap.
- **−** Self-signed = a one-time trust step per device (or a local-CA install). **−** A second port during
  transition. **−** Cert/key are new secrets to hold + back up (gitignored, 0600, on the dictator; fold into
  `dictator-files.manifest` as `local`/`preposition`).

## Rejected alternatives
- **External CA / Let's Encrypt:** needs internet + a public name; violates the air-gap target.
- **Opaque tokens + server-side session store:** more moving parts than stateless HS256 JWT for one box.
- **Reverse proxy (Caddy/nginx) for TLS:** viable, but a new always-on service; uvicorn-native TLS is fewer
  parts on a single box. Revisit if we later need HTTP/2 or multi-service fronting.

## Implementation slices
1. **DONE** — `auth_tokens.py` + tests: pure JWT mint/verify/roles/rotation + `resolve_role` dual-verify.
2. **DONE (LIVE on 210)** — `gen_tls.py` + HTTPS:8443 (self-signed, Hugh's pick); cert/VAPID folded into
   `dictator-files.manifest`.
3. **PARTIAL** — `/auth/login` + `/auth/refresh` + dual-verify wired in `main.py` (a valid HS256 JWT OR the
   legacy SHA bearer; legacy = admin). Routers stay **admin-gated** (back-compat; an admin JWT now satisfies
   them). *Remaining:* thread a min-role through the router factories for the operator/viewer split — needs
   the per-route role matrix + a decision on whether the currently-OPEN reads get gated behind `viewer`.
4. PWA: login via `/auth/login`, use `crypto.subtle` on HTTPS (keep the JS-SHA fallback for plain :8123).
   *(Web-push flip is DROPPED — see [air-gap-notify.md](../decisions/air-gap-notify.md); alerts ride MQTT.)*
5. Deprecate the legacy static bearer (after the PWA moves to `/auth/login`).
