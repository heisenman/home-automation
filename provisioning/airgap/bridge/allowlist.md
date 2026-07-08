# Web-bridge allowlist — the one sanctioned hole (hardening TODO)

The `.210` web bridge (`ha-web-bridge.conf`) is the **only** deliberate path from the internet-connected
household network into the air-gapped HA system. It is an *app-level* proxy (no IP forwarding), and auth
is end-to-end (ha-2's API enforces the admin bearer). But defense-in-depth wants the proxy itself to be
**default-DENY** — only the vetted app surface should cross.

**Current state (v1):** the proxy forwards `/` (the whole API surface) to ha-2, relying on ha-2's own
`api_authz`. Functional and layered, but not yet default-deny.

**Target allowlist (to implement in `ha-web-bridge.conf`):**
- `GET /`, `GET /assets/*`, the PWA static bundle — the dashboard UI.
- `GET /api/v1/*` — read APIs (sensors, rooms, history, weather).
- `POST /api/v1/admin/*` and the control POSTs — **only** the whitelisted control actions.
- Deny everything else (return 403), including any path not in the app surface.

Enforce with `location` blocks (exact/prefix matches) + a default `location / { return 403; }`, plus
`limit_except` to restrict methods per path. Keep the bind on `192.168.0.210` only (never `0.0.0.0`), so
the proxy is never reachable from the air-gap side. This file is the reviewable record of what crosses.
