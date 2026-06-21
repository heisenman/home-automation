# Follow-ups & clarifications for Hugh

Running list maintained during autonomous work sessions. Newest section on top. Guiding philosophies
(stated 2026-06-21): **security over the air** + **flexible modular infrastructure** between
dictator / failover / edge nodes / endpoints.

## Decisions I'd like your call on (proceeding with the noted defaults meanwhile)
1. **Unsupervised flashing** — DEFAULT: I do **not** flash the live C6 while you're away (failure could
   need a recable). All firmware is built + tested + staged; flashes wait for you. OK?
2. **Actuator config layout** — DEFAULT: separate `instance/control.yaml` (actuator devices + traits) and
   gitignored `instance/control_secrets.yaml` (per-device HMAC secrets); sensor `devices.yaml` untouched.
3. **"Confirm" second factor** for sensitive actions (unlock) — what is it concretely? PIN entered at the
   API? A physical button at an endpoint? Currently a boolean `confirmed` gate + a pluggable stub.
4. **Whole-house mode inputs** — the mechanism (authenticated set + hysteresis + pluggable input iface) is
   built; the real drivers (mains-present, UPS state, whole-house power) are TBD hardware. Which first?
5. **Per-device secret distribution** — secrets currently live in the node's gitignored `secrets.h` and a
   server-side gitignored store. Enrollment is meant to be physical-presence at the console (plan §13).
   Confirm that model (vs. e.g. a provisioning USB) before we scale past one node.

## Supervised steps queued (need you present)
- **Sign the OTA op** firmware flash (code + tests done/staged) — removes the unsigned-OTA exception.
- **C6 broker-creds** firmware flash → then the **broker auth/ACL cutover** (`provisioning/broker-auth-cutover.md`).
- **Control API** goes live only AFTER broker auth (else unauthenticated control on the API).

## Open / deferred (lower priority)
- Outdoor history read (`02` reject on attic/h_bed) — needs an app HCI-btsnoop of an attic/h_bed pull. LOW (ADR-0009).
- c_office meter **battery swap** (1–2%) — physical.
- Aranet radon live capture — needs ext-advertising scan firmware + a node near the crawlspace.
- Retire `.112` duplicate services (`sudo systemctl disable --now ha-api ha-writer mosquitto`) — your sudo.
- G11 provisioning bring-up (arrives ~2026-06-23) — your hardware step + on-device LLM.
