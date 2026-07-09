# runbook-device-verification.md — how to VERIFY a device's state/function

**Directive: trust but verify** ([AGENTS.md](AGENTS.md) principle 5). A device's state or function is a
*hypothesis* until it has been **verified live** — documented, remembered, or briefly-sampled state is not a
fact. You must verify before relying on a device's state in planning or execution, **even if it costs more
time and tokens.** Any doc that asserts a device's state/stance must link the matching entry here.

This runbook is **durable and separate from the churn**: the design log / checkpoints record *what happened*;
this file records *how to re-check it*. Keep verification procedures here, not inline in the design log.

## How to use
- **Reading a claim about a device?** Find its entry, run the *How to verify* steps, compare to *Expected*.
- **Writing a claim about a device state** (design log, checkpoint, manifest, ADR)? Add/refresh an entry here
  and link it.
- Each entry has four fields: **Claim** (the asserted state) · **How to verify** (exact commands) ·
  **Expected** (what a healthy result looks like) · **Cadence & gotchas** (what makes a naive check lie).

> **Universal gotcha — cadence before absence.** A short MQTT/broker sample proving "no data" proves nothing
> unless it exceeds the device's *update cadence*. Radon, PM, and other slow sensors update on multi-minute
> intervals; a 30–90 s window lands in the gaps. Always check the expected cadence before concluding a device
> is silent/dead/redundant. (This runbook exists because that exact mistake was made on the radon relay.)

---

## Entry 1 — Radon (`radon_crawlspace`) is recorded on the air-gap (1.0) system

**Claim:** `radon_crawlspace` (Aranet Radon Plus, SAF Tehnika, mfr company `0x0702`, BLE5 *extended*
advertising) is being recorded live on ha-2, and its source is **ha-2's own passive BLE scanner**
(`server/ingest/scanner.py`) — **not** the office-closet W5500 edge node (`hbed_s3`).

**Verified (2026-07-09):**
- Radon is **live**: the newest `radon_crawlspace` row's `ts` advances in step with the rest of the fleet
  (observed `18:22:43Z → 18:27:39Z` across a 5-min window).
- The W5500 (`hbed_s3`) is **NOT** the source: a 300 s capture of `home/edge/hbed_s3/#` saw **0 adv frames**
  (only its `status` heartbeat) while radon advanced — so its Aranet decode is redundant on 1.0, not the path.
- `scanner.py` decodes Aranet `0x0702` ext-adv and publishes decoded readings to `home/<area>/<device_id>/state`
  (a different topic tree than the edge nodes — which is why an edge-topic watch never sees radon).
- **Positive source-ID (scanner caught publishing live):** _confirming via step-2 capture — see below._

**How to verify:**
```bash
# ha-2 (192.168.1.210). Broker creds: instance/mqtt.env (HA_MQTT_USER/PASS).
# 1) Is radon fresh? (ts is an ISO-8601 STRING, not epoch — don't do unixepoch math on it)
sqlite3 instance/db/hot.db \
  "select ts, value from readings where device_id='radon_crawlspace' and metric='radon_bqm3' order by rowid desc limit 3;"
#    → compare newest ts to `select max(ts) from readings;` — within a cadence interval == live.

# 2) Catch the SOURCE publishing (retained value arrives immediately; a fresh one within ~5 min proves live):
set -a; . instance/mqtt.env; set +a
mosquitto_sub -h 127.0.0.1 -u "$HA_MQTT_USER" -P "$HA_MQTT_PASS" -t 'home/+/radon_crawlspace/state' -F '%I | %t | %p' -W 330

# 3) Confirm it is NOT an edge relay (W5500 hbed_s3): should show only `status`, no `/adv`, over 5 min:
mosquitto_sub -h 127.0.0.1 -u "$HA_MQTT_USER" -P "$HA_MQTT_PASS" -t 'home/edge/hbed_s3/#' -F '%I | %t | %p' -W 330

# 4) Sanity: the scanner service is up.
systemctl is-active ha-scanner
```

**Expected:** step 1 → newest radon `ts` within ~one cadence of `now`; step 2 → a `home/crawlspace/radon_crawlspace/state`
message, and `post` DB ts advances during the window; step 3 → `hbed_s3` publishes only `status` (0 `/adv`);
step 4 → `active`.

**Cadence & gotchas:**
- Radon updates every **few minutes** (`scanner.py` `REPUBLISH_INTERVAL_S`, or a change > `1.0 Bq/m³`). A
  window under ~5 min can legitimately show nothing — **not** evidence of a dead sensor.
- `ha-scanner` logs **nothing per-reading** (quiet passive scan); an empty `journalctl -u ha-scanner` is
  normal and is **not** evidence it isn't hearing the device. Verify by the published `/state` topic, not logs.
- `hot.db.readings.ts` is a TEXT ISO-8601 string — `datetime(ts,'unixepoch')` / epoch subtraction gives
  nonsense (a ~56-year "age"). Compare ISO strings directly or via `strftime('%s', ts)`.
- ha-2's `instance/hot.db` (no `/db/`) is a 0-byte decoy — the real store is **`instance/db/hot.db`**.
