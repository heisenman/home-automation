# Relaying the Aranet from .245 → ha-dev (mosquitto bridge)

**Goal:** ha-dev (`192.168.0.210`) shows the crawlspace `aranet_radon` without scanning it
locally. ha-dev can hear the unit (active probe: **−70 dBm**, `F4:37:5A:68:9F:1A`) but runs
its BLE scanner in **passive low-radio** mode, which does **not** deliver the Aranet's BLE5
**extended advertising** — and we're keeping passive (active mode would re-open MT7922
stability, validated 2026-06-24). So `.245`, which already decodes the Aranet and holds its
history, relays the reading to ha-dev as an "edge node."

> **Status:** SPEC — not applied. Touches **`.245`** (the live dictator), so apply **with Hugh
> present**, deliberately. Additive only (a new outbound bridge); it does not change how `.245`
> ingests or controls anything. Standing rule: don't disrupt `.245`.

---

## Why a mosquitto bridge (not the `home/edge/...` envelope)

ha-dev's hot writer subscribes to **`home/+/+/state`** (`server/storage/writer.py:49`) and
persists every reading. `.245`'s `ha-scanner` already publishes the Aranet to the canonical
topic **`home/crawlspace/aranet_radon/state`**. So forwarding that one topic verbatim lands it
straight in ha-dev's DB + dashboard — no re-wrapping, no `ha-edge-mapper` hop. (The
`home/edge/<node>/<mac>/adv` path exists for *raw* relays that still need MAC→identity mapping;
here `.245` has already decoded + identified it, so the direct bridge is simpler.)

**Scope it to the Aranet ONLY.** ha-dev already scans the 10 SwitchBot meters locally — bridging
a wildcard like `home/+/+/state` would double-write those. Forward exactly the one Aranet topic.

---

## The change on `.245`

Add a drop-in (e.g. `/etc/mosquitto/conf.d/bridge-ha-dev.conf` — confirm `.245`'s actual
drop-in dir; the repo's `server/config/mosquitto.conf` is itself a drop-in):

```conf
# Outbound bridge: relay the crawlspace Aranet to ha-dev (.210) so the dev box
# shows radon/CO2/etc. without scanning the unit locally. Additive; out-only.
connection ha-dev-aranet
address 192.168.0.210:1883
bridge_protocol_version mqttv311
cleansession true
notifications false
start_type automatic
restart_timeout 30
keepalive_interval 60

# .245 (local) -> ha-dev (remote). QoS 0 is fine for the ~1 Hz, rate-limited state.
topic home/crawlspace/aranet_radon/state out 0
```

Notes:
- **No remote credentials** — ha-dev's broker is anonymous. If ha-dev ever gains auth, add
  `remote_username` / `remote_password` here.
- **Out-only, single topic** → no loop risk (ha-dev never publishes `aranet_radon`, so nothing
  echoes back). Never make this bidirectional.
- **`.245` auth/ACL:** the broker auth cutover is live on `.245`
  (`provisioning/broker-auth-cutover.md`). A bridge `connection` is **internal to the broker**,
  so its outbound forward is not gated by the local `acl` file — no ACL stanza needed. Just place
  the drop-in where `.245`'s mosquitto loads configs (alongside the auth drop-in).

---

## Apply (with Hugh, on `.245`)

1. **Confirm the exact source topic on `.245`** (area slug must match its registry — this spec
   assumes `crawlspace`):
   ```bash
   mosquitto_sub -h localhost -t 'home/+/aranet_radon/state' -v   # on .245; note the real topic
   ```
   If the area differs, edit the `topic` line to match.
2. **Drop in the config** above.
3. **Restart the broker** so the new `connection` block instantiates (SIGHUP/reload does not
   reliably add a bridge in mosquitto 2.x):
   ```bash
   sudo systemctl restart mosquitto      # on .245
   ```
   `.245`'s own services (scanner/writer/edge/controller) auto-reconnect within seconds. This is
   the one brief blip — hence "with Hugh present."

## Verify (on ha-dev)

```bash
mosquitto_sub -h localhost -t 'home/crawlspace/aranet_radon/state' -v   # relayed readings arrive
curl -s http://localhost:8123/api/v1/sensors | grep -o aranet_radon     # device now present
```
Sensor count goes **10 → 11**; `aranet_radon` appears in `/devices`, `/api/v1/sensors`, and the
dashboard, labelled from ha-dev's registry (which already holds `F4:37:5A:68:9F:1A` →
`aranet_radon`, area `crawlspace`). No per-device UI work.

## Rollback

Delete the drop-in and `sudo systemctl restart mosquitto` on `.245`. ha-dev simply stops getting
new Aranet readings; nothing else is affected.

---

## When `.245` is decommissioned

This bridge retires with `.245`. At that point the Aranet needs a new in-range source feeding
ha-dev directly — the ESP32-C6 Wi-Fi relay in `edge/esp32c6/dev-box-relay.md` (placed near the
crawlspace), pointed at ha-dev's broker. Until then, the `.245` bridge is the path.
```
