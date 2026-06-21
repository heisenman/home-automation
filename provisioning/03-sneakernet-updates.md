# Sneakernet Update Architecture (air-gapped OS + software lifecycle)

The server runs **air-gapped**. All OS, package, Python, source, driver, and data updates arrive on a
**USB "transfer bundle"** prepared on a **connected twin** and verified on the server before applying.
This is the same trust model as the existing Parquet hash-verification (`docs/adr/ADR-0004`).

```
   ┌──────────────────────────┐   build + sign bundle    ┌──────────────────────────┐
   │  CONNECTED TWIN          │ ───────► USB ──────────►  │  AIR-GAPPED HA SERVER    │
   │  (online; SAME arch+OS:  │                           │  verify → stage → apply  │
   │   Debian13/amd64/py3.13) │ ◄──────  USB ◄──────────  │  (data export for backup)│
   └──────────────────────────┘   data/weather return     └──────────────────────────┘
```

The **twin must match the server** — Debian 13, `amd64`, Python 3.13 — or binaries/wheels won't be ABI-
compatible. The twin can be a VM, a container, or (ideally) the **failover G11 while still online**.

---

## Bundle layout & integrity

```
ha-bundle-YYYYMMDD/
├── MANIFEST.sha256          # sha256 of every file below (the trust root)
├── MANIFEST.sha256.sig      # optional: detached signature (see §trust)
├── apt/                     # .deb packages (or a snapshot-mirror tree) + Packages index
├── wheelhouse/              # cp313/amd64 wheels for requirements.txt
├── src/                     # vendored source tarballs + their upstream checksums
├── drivers/                 # r8125-dkms source, firmware blobs
├── kernel/                  # linux-image/linux-headers .deb (if updating kernel)
├── git/                     # repo.bundle (git bundle of the app repo)
└── data/                    # weather rows in, sensor exports out (see §data)
```

**On the server, always verify before touching anything:**
```bash
cd /media/usb/ha-bundle-YYYYMMDD
sha256sum -c MANIFEST.sha256        # MUST pass — abort on any FAILED line
# (optional) verify signature: see §trust
```
Generate the manifest on the twin with:
`find . -type f ! -name MANIFEST.sha256 -print0 | sort -z | xargs -0 sha256sum > MANIFEST.sha256`.

---

## §apt — OS & system packages offline

**Recommended: pinned snapshot mirror.** Reproducible point-in-time Debian state, identical on prime +
failover.

On the twin (online):
```bash
# Mirror just what you need from a dated snapshot (full mirror is large; or use a partial mirror tool)
#   https://snapshot.debian.org/  ->  pick a date, e.g. 20260601T000000Z
# Simple approach for a handful of upgrades — download the exact .debs you need:
apt-get -o Dir::Cache::archives="$PWD/apt" --download-only install --reinstall <pkgs>
cd apt && dpkg-scanpackages . /dev/null | gzip -9c > Packages.gz
```

On the server (offline):
```bash
# add the bundle as a file:// repo, pinned, no network
echo "deb [trusted=yes] file:/media/usb/ha-bundle-YYYYMMDD/apt ./" | sudo tee /etc/apt/sources.list.d/ha-bundle.list
sudo apt-get -o Acquire::Languages=none update
sudo apt-get install --only-upgrade <pkgs>     # or install new ones
sudo rm /etc/apt/sources.list.d/ha-bundle.list # remove the bundle repo afterward
```
For full-mirror reproducibility instead of cherry-picked debs, use `apt-mirror`/`aptly` against a
snapshot date on the twin and rsync the tree into `apt/`.

**Alternative:** `apt-offline` (`apt-offline set` on server → fetch on twin → `apt-offline install`).
Good for one-off upgrades; the snapshot mirror is better for reproducibility.

---

## §python — wheelhouse offline

On the twin (Debian 13 / py3.13 — MUST match):
```bash
pip download -r requirements.txt -d wheelhouse/
# captures cp313/manylinux wheels + sdists for anything without a wheel
```
On the server (offline):
```bash
venv/bin/pip install --no-index --find-links /media/usb/ha-bundle-YYYYMMDD/wheelhouse -r requirements.txt
```
- Keep `requirements.txt` the single source of truth; regenerate the wheelhouse whenever it changes.
- For stronger integrity, pin hashes (`pip download` + `pip install --require-hashes`).
- If a package lacks a cp313 wheel, the sdist is included — ensure `build-essential`+headers are present
  so it compiles on the server (or pre-build the wheel on the twin and ship that).

---

## §source — compiled components (BlueZ, mosquitto, kernel)

For anything source-compiled (spec §4), vendor the **tarball + upstream checksum + a build script**:
```
src/bluez-5.xx.tar.xz        src/bluez-5.xx.tar.xz.sha256     src/build-bluez.sh
src/mosquitto-2.x.x.tar.gz   src/mosquitto-2.x.x.tar.gz.sha256 src/build-mosquitto.sh
```
Each `build-*.sh` is deterministic (`./configure --prefix=/usr/local ... && make && sudo make install`)
and records the exact flags (e.g. BlueZ `--enable-experimental`, any `znver1` CFLAGS). The server builds
from the vendored tarball — never fetches. Keep the previous install for rollback (`make uninstall` or a
versioned `/usr/local` prefix).

---

## §drivers & firmware

- **r8125 (2.5GbE fallback):** ship the Realtek `r8125` DKMS source in `drivers/`. Install only if the
  in-tree `r8169` proves unstable (spec §1): `sudo dkms add ./r8125-x.x && sudo dkms install r8125/x.x`.
- **firmware:** if a new device needs blobs, include the matching `firmware-*` .deb or raw files for
  `/lib/firmware`. The UB500 (RTL8761B) firmware is in `linux-firmware` already.

## §kernel

Ship `linux-image-*` + `linux-headers-*` `.deb` in `kernel/`. Install offline via the apt file:// repo.
Keep the prior kernel installed (GRUB fallback) until the new one is verified across a reboot. A
`znver1`-tuned custom kernel, if you pursue it, lives as a source tarball + `build-kernel.sh` in `src/`.

## §git — app code offline

On the twin: `git bundle create git/repo.bundle --all` (or `main`).
On the server:
```bash
git -C ~/home_automation pull /media/usb/ha-bundle-YYYYMMDD/git/repo.bundle main
```
This carries app changes (new ADRs, dashboard, tools) without GitHub. Instance PII still travels
separately in `data/` and never enters the bundle's git history.

---

## §data — bidirectional transfer (weather in, sensors out)

Per `docs/adr/ADR-0008-weather-lane.md`, the air-gapped weather source reads rows synced during backup
rather than calling Open-Meteo:
- **Inbound:** twin fetches recent Open-Meteo history → `data/weather-in.parquet`; server imports into
  `weather.db` (idempotent `INSERT OR IGNORE`, same as the live lane).
- **Outbound:** server exports new sensor rows / daily summaries → `data/sensors-out.parquet` for
  off-box backup. The existing hash-manifest tooling covers integrity.

Switch the source in `server/weather/__main__.py:build_source()` from `OpenMeteoSource` to the
transfer-reading source; the store and runner are unchanged.

---

## §trust & cadence

- **Integrity:** `MANIFEST.sha256` is mandatory and checked on every apply. For tamper-resistance,
  sign it: on the twin `gpg --detach-sign MANIFEST.sha256`; on the server `gpg --verify` against a
  pre-installed public key. (`[trusted=yes]` on the file:// repo is acceptable only because the bundle
  itself is verified first.)
- **Cadence:** batch updates monthly (or on a security trigger). Smaller, frequent bundles are easier
  to verify and roll back than big-bang updates.
- **Rollback:** keep the prior kernel, prior `/usr/local` source installs, and a `venv.prev` snapshot
  so any apply is reversible without a network. Test bundles on the **failover/twin first**, then prime.
- **Two-box parity:** apply each bundle to prime and failover; `rsync` `/srv/ha-data/db/` prime→failover
  so promotion is always warm.

---

### Quick reference — apply a bundle
```bash
cd /media/usb/ha-bundle-YYYYMMDD && sha256sum -c MANIFEST.sha256   # 1. verify (abort on fail)
# 2. apt:    add file:// repo → apt update → install/upgrade → remove repo
# 3. python: pip install --no-index --find-links wheelhouse -r requirements.txt
# 4. source: run src/build-*.sh as needed
# 5. git:    git pull git/repo.bundle main
# 6. data:   import weather-in, export sensors-out
# 7. verify: provisioning/02-full-server-spec.md §9 checklist, then reboot-test
```
