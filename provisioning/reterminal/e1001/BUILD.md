# E1001 — build & OTA (bench)

The E1001 is an **ESPHome** node (unlike the D1001 beachhead, which is ESP-IDF). It builds on the ops
bench via the **esphome docker image** — there is intentionally **no host esphome CLI**; the image carries
the toolchain and writes its `.esphome/` cache into this dir (gitignored). `secrets.yaml` (gitignored,
already present on the bench) supplies wifi/ota creds.

Run from **this directory** (`provisioning/reterminal/e1001/`):

```sh
# compile only (validate a change without touching the device)
docker run --rm --network host -v "$PWD":/config ghcr.io/esphome/esphome:latest compile e1001.yaml

# compile + OTA to the live node (.71)
docker run --rm --network host -v "$PWD":/config ghcr.io/esphome/esphome:latest upload e1001.yaml --device 192.168.0.71

# stream logs (diag)
docker run --rm --network host -v "$PWD":/config ghcr.io/esphome/esphome:latest logs e1001.yaml --device 192.168.0.71
```

- Image: `ghcr.io/esphome/esphome:latest` (present on the bench; `docker` works without sudo).
- Diag/telemetry bus: MQTT topic `e1001-bench/#` (rename to `e1001-<area>/#` at deploy — Step 6).
- Profile/offset re-push is a **data** change (`battery_profile_v1.json`) delivered over MQTT — **no reflash**.
- Everything (config, `components/`, fonts, secrets, cache) lives here on the bench; nothing is sourced
  from the `//192.168.0.245` share (that's just a checkout copy of the same git-tracked source).
