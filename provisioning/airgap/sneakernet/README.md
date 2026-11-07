# Sneakernet — air-gap backup OUT + updates IN (ADR-0033 Phase 4)

The air-gap dictator (ha-2) never touches the internet, so its data leaves — and vetted updates arrive — via a
controlled, offline, **auditable USB round-trip**. This is the default-on half of the "build BOTH" directive
(the other half, the continuous relay, is Phases 1–3). See `provisioning/03-sneakernet-updates.md` for the full
bundle spec.

## Backup OUT (data-of-record → USB)
```bash
# on ha-2, with a USB mounted at e.g. /media/HA-BACKUP:
provisioning/airgap/sneakernet/backup.sh /media/HA-BACKUP
# far side (or before restore): verify integrity, optionally restore
provisioning/airgap/sneakernet/verify-restore.sh /media/HA-BACKUP/ha2-backup-<stamp>
provisioning/airgap/sneakernet/verify-restore.sh /media/HA-BACKUP/ha2-backup-<stamp> --restore /path/to/ha-root
```
- Consistent sqlite snapshots (`.backup`) for hot/weather/control/rungs/quarantine + the parquet archive +
  config-of-record (`devices.yaml`, `weather.env`, `control.yaml`).
- Every file sha256'd into `MANIFEST.sha256`; restore refuses on a bad manifest.
- Records `instance/.last-sneakernet-backup` so the **nag** (`backup-nag.sh`, weekly timer) alerts when the
  only off-box copy is overdue.

## Updates IN (vetted packages) — and the `.210` mirror
- The **USB may also carry** vetted `.deb`/wheel/`tzdata`/CA bundles IN (per the bundle spec) — sneakernet is
  the **default** update path.
- A `.210` apt/pip **mirror** is the alternative (Q3), but it **ships DISABLED by default in git** — updates
  are an operator-initiated maintenance-window event, never an ambient capability. (Mirror config is
  deliberately not committed in an enabled state; enable it explicitly when needed.)

### Built: the `.210` apt relay (2026-11-07)
`tools/ha2_apt_relay.py` (on `.210`) computes exactly what ha-2's `apt-get dist-upgrade` needs from `.210`'s fresh
lists (same Debian 13/amd64 and sources, checked), downloads only those `.deb`s, and lays them out as a minimal
file: repository beside Debian's signed InRelease/Packages files, plus `apply.sh` (= `tools/apt_bundle_apply.sh`)
and a `MANIFEST.sha256`. It ships the bundle to `~/apt-bundle-<stamp>` on ha-2 (or `--out <USB>` for sneakernet).
Nothing installs until Hugh runs, on ha-2: `sudo ~/apt-bundle-<stamp>/apply.sh` — manifest check, failover inhibit
(refreshed), apt update + dist-upgrade from the bundle in a private lists/cache dir, then it ASKS to reboot when a
newer kernel is installed (yes: inhibit cleared, reboot, the VIP moves to the standby and back; no: active `ha-*`
services restarted). Trust: ha-2's own apt verifies Debian's signatures with ha-2's keyrings, so `.210` is only a
courier (tamper-tested 2026-11-07: a corrupted `.deb` fails both apt's hash check and the manifest). Run
`sudo apt update` on `.210` first; apply before the printed deadline (the security InRelease's Valid-Until, ~1 week).
`--dry-run` lists the packages; `--target local` builds for `.210` itself (tests the apply/reboot path).

## Cert rotation (D1)
`provisioning/airgap/sneakernet/rotate-cert.sh` regenerates the long-dated self-signed TLS cert
(`tools/gen_tls.py --days 3650`) and stages it for redeploy — pairs with the `ha-cert-monitor` T-12mo alert.
