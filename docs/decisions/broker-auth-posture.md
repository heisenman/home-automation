# Broker auth/ACL posture — recommendation (ops, 2026-06-24)

*Task `broker-auth-posture`. Analysis + recommendation by `ops`; the call is Hugh's (it couples with the
air-gap roadmap). Decision-prep, not a unilateral change.*

## Current state
- **210 (dictator) broker:** anonymous — `allow_anonymous true`, `listener 0.0.0.0:1883`, no `password_file`.
- **`.245` (standby) broker:** authenticated — `password_file`, `allow_anonymous false` — plus the
  cluster-bus bridge out to 210, and `instance/mqtt.env` holds the `dictator` identity.
- **What currently relies on 210 being anonymous:** the coordination bus (`ha/agents/#` + `coord.py` + the
  wake watcher), the cluster bus reads (`ha/cluster/#`, `cluster-doctor`), and any edge node pointed at 210.

## The real question
Actuation is **already cryptographically gated** — every device directive is HMAC-signed per-device
(ADR-0010), so a forged/unsigned `home/<area>/<dev>/cmd` is rejected *at the node* regardless of broker
auth. So broker auth is **not** what protects control. What anon *does* leave open on a shared LAN:
1. **Telemetry injection** — anyone on the LAN can publish fake `home/.../state`, which the controller would
   treat as real → bad automation decisions. This is the main residual risk.
2. Eavesdropping on all telemetry, and bus disruption.

## Recommendation: **stay anonymous now; add broker auth as part of `network-init` when the air-gap lands**
Rationale:
- **The air-gap is the bigger boundary.** Once the OpenWRT router gives us an air-gapped network
  (`network-roadmap`), only trusted devices are on it — which closes the telemetry-injection risk far more
  completely than per-client passwords on a still-shared LAN.
- **Lockdown is all-or-nothing, so do it once, not piecemeal.** The moment 210 flips to authed, *every*
  client without creds breaks at once: server services, every edge node, `coord.py`, the wake watcher,
  `cluster-doctor`, and the `.245→210` bridge (which would need `remote_username/password`). Doing that as a
  standalone change now is high-friction for low marginal gain over the crypto we already have.
- **Provision identities in one cutover** when we do it (mirrors `.245`'s proven scheme): a `dictator`
  identity for server services (already in `mqtt.env`), per-node identities for edge nodes (via the enroll
  flow), and a dedicated **`coord` read/write identity** for the agent + cluster + doctor tooling on
  `ha/agents/#` + `ha/cluster/#`. Bake it into `network-init-tooling` so a fresh network comes up authed.

## If Hugh wants it sooner (the one trigger)
If the **current** LAN has untrusted devices that could inject telemetry, that's the reason to not wait —
prioritize either the air-gap or an interim broker-auth cutover. Absent that, the crypto + the upcoming
air-gap make anon an acceptable, documented risk in the meantime.

## Net
Couple this to **`openwrt-router-onboard` → `network-init-tooling`**, not a standalone task. No change to 210
today; revisit at air-gap. (If you disagree and want it now, it's a ~1-session cutover but touches every
client — say the word and I'll sequence it so nothing drops.)
