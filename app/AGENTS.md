# app/ — native client shells

*↑ The by-location node for `app/` in the [root AGENTS.md](../AGENTS.md) tree (ADR-0021/0025); the
by-capability index is [`docs/REUSE.md`](../docs/REUSE.md). Link up, don't duplicate.*

Native wrappers around the **existing PWA** — not alternative UIs. Governed by
[ADR-0038](../docs/adr/ADR-0038-native-mobile-shell.md).

| Path | What it is | When to touch |
|------|------------|---------------|
| `android/` | Android shell: `WebView` + pinned trust + MQTT alert foreground service | Shell behaviour, cert rotation, alert delivery |

## The one thing to know before editing here

**The UI is served, not bundled.** The shell loads `https://<endpoint>/app/` at launch; the APK contains no
web assets. A PWA change deploys to ha-2 and reaches every phone with no rebuild — which is the same
"updatable without reflashing" property ADR-0019 requires of the panel clients.

So: **UI work belongs in `server/web/`, not here.** If you find yourself wanting to add a screen to the
Android app, the question to ask first is whether it belongs in the PWA, where the browser and the D1001
panel would get it too. This tree is only for what a browser genuinely cannot do:

1. **Trust** — the server cert is self-signed, and browsers refuse to register a service worker under one
   (`docs/decisions/air-gap-notify.md`). The app pins it instead.
2. **Background alerts** — a long-lived MQTT subscription to `home/_alerts` / `home/_alert/new`. This is the
   "LAN consumer" that the air-gap-notify decision left open.
3. **App identity** — launcher icon, own task, sandboxed storage.

## Gotchas

- **The pinned cert is a frozen copy** of `instance/tls/server.crt`. Rotate the server cert without
  rebuilding + reinstalling and every phone fails closed. `tests/test_android_cert_pin.py` guards it,
  including a 90-day expiry tripwire.
- **The signing key is permanent.** Android will not update an app whose signature changed. See
  [`docs/SECRETS.md`](../docs/SECRETS.md); back it up off the box.
- **`specialUse`, not `dataSync`,** for the foreground service — Android 15 caps `dataSync` at ~6h/day and
  bars it from starting at boot, which would silently mute the alert lane.
- **Build on `.210` only.** ha-2 has no JDK and no Android SDK, exactly as it has no ESP-IDF.

Full build/sign/install guide: [`docs/app/ANDROID.md`](../docs/app/ANDROID.md).
