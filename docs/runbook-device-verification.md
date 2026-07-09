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

> **Universal gotcha 1 — right topic before absence.** "No data" on a broker watch means nothing if you're on
> the wrong topic tree. ha-2's *scanner* publishes decoded BLE readings to `home/<area>/<device_id>/state`;
> *edge nodes* relay to `home/edge/<node>/<mac>/adv`. Subscribing to one while the data flows on the other reads
> as silence. (This is what first misled the radon investigation.)
> **Universal gotcha 2 — cadence before absence.** Even on the right topic, a sample shorter than the device's
> *republish interval* can legitimately show nothing. Check the expected cadence before concluding silent/dead.

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
- **Positive source-ID — CONFIRMED live:** a 5-min watch of `home/+/radon_crawlspace/state` caught the scanner
  publishing **6 fresh frames, one every ~60 s** (`18:30:40…18:34:41Z`), `ts` advancing each time, value moving
  (10→24 Bq/m³ over the session), `meta.mac F4:37:5A:68:9F:1A`, `rssi -86`; `hot.db` advanced in lockstep. The
  W5500 stayed silent throughout. Source = ha-2 `scanner.py`, definitively; `hbed_s3` radon decode is redundant.

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
- Radon republishes **~60 s** (`scanner.py` `REPUBLISH_INTERVAL_S`; or sooner on a change > `1.0 Bq/m³`) —
  verified: 6 frames in 5 min. A watch under ~90 s can still miss one, so give it ≥2 min on the right topic.
- `ha-scanner` logs **nothing per-reading** (quiet passive scan); an empty `journalctl -u ha-scanner` is
  normal and is **not** evidence it isn't hearing the device. Verify by the published `/state` topic, not logs.
- `hot.db.readings.ts` is a TEXT ISO-8601 string — `datetime(ts,'unixepoch')` / epoch subtraction gives
  nonsense (a ~56-year "age"). Compare ISO strings directly or via `strftime('%s', ts)`.
- ha-2's `instance/hot.db` (no `/db/`) is a 0-byte decoy — the real store is **`instance/db/hot.db`**.
