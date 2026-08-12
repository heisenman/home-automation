# Android app — build, sign, install

The phone app is a **thin shell** around the existing PWA: a `WebView` pointed at the house, plus the
three things a browser cannot do here — trust the self-signed certificate, hold a background MQTT
subscription for alerts, and be an app. Design and rationale: [ADR-0038](../adr/ADR-0038-native-mobile-shell.md).

**The UI is not in the APK.** It is fetched from the server at launch, so a PWA change deploys to ha-2 and
every phone has it — no rebuild, no reinstall. You only rebuild the app when the *shell* changes or the
*certificate* rotates.

```
app/android/
├── setup-toolchain.sh      one-time: JDK 21 + Android SDK + Gradle
├── build.sh                build.sh [debug|release]
├── app/src/main/
│   ├── java/house/homeauto/app/
│   │   ├── MainActivity.kt      WebView host, reachability, error screen
│   │   ├── SettingsActivity.kt  endpoint + alerts toggle
│   │   ├── AlertService.kt      MQTT foreground service
│   │   ├── Alerts.kt            payload parsing (pure, unit-tested)
│   │   ├── Endpoints.kt         URL/host derivation (pure, unit-tested)
│   │   └── Notifications.kt     channels by severity
│   └── res/
│       ├── raw/ha_server.crt            the pinned certificate
│       └── xml/network_security_config.xml
└── app/src/test/               JVM unit tests — no device needed
```

## Build

```bash
app/android/setup-toolchain.sh      # once per build host
app/android/build.sh                # debug APK
app/android/build.sh release        # signed release APK
```

Build host is **.210**. ha-2 does not build anything — it has no JDK and no Android SDK, exactly as it has
no ESP-IDF. Output:

| Variant | Path | Use |
|---|---|---|
| debug | `app/android/app/build/outputs/apk/debug/app-debug.apk` | bench testing only — it is `debuggable`, so anyone with USB access can attach to a process holding the admin token |
| release | `app/android/app/build/outputs/apk/release/app-release.apk` | **what goes on a phone** |

`build.sh` re-copies `instance/tls/server.crt` into the app on every build, so you cannot accidentally
build against a stale pin. `tests/test_android_cert_pin.py` is the backstop for committing without building.

## Install (side-load)

1. Get the APK onto the phone — USB, or serve it from `.210` (see *Hosting*, below).
2. On the phone, opening the APK prompts to allow installing from that source. Allow it once.
3. Install. On first launch, grant the **notifications** permission when asked — without it the alert
   service still runs but stays mute.
4. If the house is not at the default address, long-press the app icon → **Server settings**.

Nothing else is required. No developer mode, no ADB, no account.

## Signing

Release key: `instance/android-release.keystore`, alias `ha-shell`, RSA-4096, passwords in
`instance/android-release.properties` (both gitignored, `0600`, indexed in [SECRETS.md](../SECRETS.md)).

> **Back the keystore up off this box.** Android will not install an update whose signature differs from
> the installed app. Lose it and every phone must uninstall — losing its settings — to move forward.
> v2 **and** v3 signing are enabled; v3 is the scheme that carries proof-of-rotation, which is the only
> graceful path out of a lost or compromised key.

To recreate it (only if starting over — this invalidates every installed copy):

```bash
keytool -genkeypair -v -keystore instance/android-release.keystore \
  -alias ha-shell -keyalg RSA -keysize 4096 -validity 10000 \
  -dname "CN=Home Automation, OU=Household, O=Home Automation, L=Portland, ST=OR, C=US"
```

## Endpoints and the certificate

The app talks to one of two addresses, both of which are in the server cert's SAN list:

| | Address | What it is |
|---|---|---|
| **Primary** | `https://192.168.0.210` | the nginx bridge on `.210`, proxying to the air-gapped dictator ha-2. The production path. |
| **Fallback** | `https://192.168.0.200:8443` | the VIP — whichever node currently holds it. |

`192.168.1.200` (ha-2 direct) is deliberately **not** offered: it is on the air-gap leg, unroutable from a
phone, and absent from the cert SANs, so the pin would reject it anyway.

The app trusts **only** `res/raw/ha_server.crt` for those two hosts — not the system CA store. A different
certificate on that address fails closed, and the app says so rather than offering to continue. That is the
point: it is why there is no interstitial and no per-device CA profile.

### When the certificate rotates

The current one expires **2028-04-05** (ADR-0033 lists TLS expiry among the air-gap lifecycle time-bombs).
`tests/test_android_cert_pin.py` fails the suite 90 days out, so this should never arrive as a surprise.

1. Rotate `instance/tls/server.crt` on the server as usual.
2. `app/android/build.sh release` — the new cert is copied in automatically.
3. **Reinstall on every phone.** Until a phone takes the new APK it cannot connect at all.

Step 3 is the whole cost of pinning. It is worth it, but it is not free, and it is why the pin is scoped to
two hosts rather than the whole LAN.

## Alerts

The app subscribes to the same bus the wall panels read (`docs/decisions/air-gap-notify.md`):

- `home/_alerts` — retained snapshot. Delivered on connect; used to **withdraw** notifications for alerts
  that have since resolved, so the shade shows the house and not its history.
- `home/_alert/new` — one publish per newly-appeared alert; the server already does the edge detection, so
  each of these becomes a notification.

Severity maps to notification **channels**, so the household governs them from Android settings rather than
from a preferences screen we would have to build:

| Channel | Importance | Contains |
|---|---|---|
| Critical alerts | HIGH, bypasses Do Not Disturb | tank full, sensor dead, battery critical |
| Warnings | DEFAULT | low battery, sensor stopped reporting |
| Notices | LOW | override about to expire |
| Connection status | MIN | the permanent notice Android requires of a foreground service |

The service type is `specialUse`, not `dataSync`: Android 15 caps `dataSync` at ~6 cumulative hours a day
and bars it from starting at boot. Either would make the alert lane go quiet **without saying so**.

**This is home-Wi-Fi only.** Off the home network there is nothing to reach; the service backs off and
reconnects when the phone rejoins.

## Hosting the APK (optional)

To install without a cable, serve the release APK from the existing nginx on `.210`. This adds a
household-LAN-facing path, so apply it deliberately:

```nginx
# /etc/nginx/conf.d/ha-web-bridge.conf, inside the existing 192.168.0.210:443 server block
location /download/ {
    alias /home/visko/home_automation/instance/download/;
    autoindex on;
}
```

Then `install -m 644 app/android/app/build/outputs/apk/release/app-release.apk instance/download/` and open
`https://192.168.0.210/download/` on the phone. Note the phone will show a certificate warning **in the
browser** for that download — the pin lives inside the app, and the browser has never trusted this cert.

## Tests

```bash
app/android/gradlew -p app/android :app:testDebugUnitTest   # 22 JVM tests, no device
venv/bin/python -m pytest tests/test_android_cert_pin.py    # 5 tests, part of the main suite
```

The JVM tests cover the two pure surfaces — endpoint normalisation and alert parsing — against the real
published payload shapes. The Python test guards the pin itself: bundled cert matches the server's, is not
near expiry, pins only hosts the cert covers, and carries no private key.

## Known limits

- **No automatic updates.** Side-loading has no update channel. Rebuild, redistribute.
- **Firmware flashing works but is odd from a phone.** It is a server-side USB path on `.210`
  (`/api/v1/flash`), not browser WebSerial, so the UI functions — it just drives a USB port in another room.
- **OEM battery management** may still throttle a long-lived socket on some skins. If alerts go quiet on a
  particular phone, exempt the app from battery optimisation in Android settings.
- **iOS is not built.** See ADR-0038 § Scope — it needs a signing identity, and would get foreground-only
  alerts because iOS suspends background sockets.
