# Self-hosted ntfy — air-gap-native phone notifications

Turns HA alerts into phone push **without any vendor cloud**: the API publishes alerts to MQTT
(`home/_alert/new`), `ha-ntfy-bridge` forwards them to a **self-hosted ntfy server** on 210, and phones run
the ntfy app pointed at that LAN server. Decided 2026-06-25 (replaces vendor Web Push, which the air gap
breaks). See [docs/decisions/air-gap-notify.md](../../docs/decisions/air-gap-notify.md).

```
 API alert loop ──MQTT home/_alert/new──> ha-ntfy-bridge ──HTTP──> ntfy (:8095 on 210) ──> phone app
   (dictator, VIP-gated)                    (VIP-gated)              (self-hosted, LAN)      (LAN subscribe)
```

## Platform reality (important)
- **Android: fully self-hosted / air-gap-clean.** The ntfy Android app holds a foreground connection to the
  *local* server and delivers instantly with no internet. This is the target case.
- **iOS: limited self-hosted.** Apple background push requires APNs (Apple's cloud), which a self-hosted
  server can only reach via ntfy.sh's relay (`upstream-base-url`) — a cloud dependency we deliberately omit.
  On a true air gap, iOS gets notifications only while the app is **foregrounded** (or via the in-app
  banner). If iOS background push matters, that's the one place a cloud relay is unavoidable — a conscious
  exception, not the default. Android is the air-gap path.

## Install on 210 (ops-staged; run by Hugh — production write to the dictator)
1. **ntfy server** (210 has internet now; for the air gap, vendor the .deb first). The GitHub
   `latest/download/` shortcut 404s — the asset name is version-stamped, so resolve it via the API:
   ```bash
   ARCH=$(dpkg --print-architecture)            # amd64 on 210
   URL=$(curl -fsSL https://api.github.com/repos/binwiederhier/ntfy/releases/latest \
           | grep -o "https://[^\"]*_linux_${ARCH}.deb" | head -1)
   curl -fsSL "$URL" -o /tmp/ntfy.deb
   sudo apt-get install -y /tmp/ntfy.deb        # provides the ntfy binary + ntfy.service
   sudo install -d /var/lib/ntfy
   sudo cp provisioning/ntfy/server.yml /etc/ntfy/server.yml
   sudo systemctl enable --now ntfy
   curl -s localhost:8095/v1/health             # {"healthy":true}
   ```
2. **The bridge** (additive; VIP-gated; never touches the control plane):
   ```bash
   sudo cp systemd/ha-ntfy-bridge.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now ha-ntfy-bridge
   systemctl is-active ha-ntfy-bridge
   ```
3. **Smoke test** — publish a fake alert event and confirm it lands in ntfy:
   ```bash
   mosquitto_pub -h localhost -t home/_alert/new \
     -m '{"schema":1,"alert":{"severity":"warning","kind":"unreachable","device_id":"meter_test","name":"Test Meter","detail":"no data for 11 min"}}'
   curl -s "localhost:8095/ha-alerts/json?poll=1" | tail -1     # should show the "Test Meter: unreachable" message
   ```

## Phone setup (per device, one-time)
1. Install **ntfy** (Play Store / App Store) or the F-Droid build.
2. Settings → **Default server** = `http://192.168.0.200:8095` (the VIP; reachable on the LAN once
   `vip-unreachable-from-wifi` is fixed at the OpenWRT cutover — until then use `http://192.168.0.210:8095`).
3. **Subscribe** to topic `ha-alerts`.
4. (Android) allow the app to run in the background for instant delivery.

## Notes
- **VIP-gating:** the bridge only forwards while its box holds the VIP, so a failover doesn't double-notify.
  For full notify-HA, run ntfy + the bridge on the standby too and point phones at the VIP — follow-up.
- **Auth:** server is anonymous-on-LAN for now (matches the broker). Add an ntfy token (set `NTFY_TOKEN` via
  `instance/ntfy.env`) when broker-auth lands at the air-gap cutover (theme D).
- **Rollback:** `sudo systemctl disable --now ha-ntfy-bridge ntfy`.
