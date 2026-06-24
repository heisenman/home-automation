# Stage 2b — Post-install finish (the no-LLM path)

After the Stage-1 ISO installs and `firstboot.sh` runs, the box has Debian + the
`visko` user + SSH + Node + Claude Code + this repo cloned. It is **not yet** a
running HA server. This page closes that gap in **one command, no Claude session
required** — it scripts everything that was done by hand when `ha-dev` was brought
up (2026-06-24), so the next box needs far less follow-on.

```bash
ssh visko@<box-ip>
cd ~/home_automation
./provisioning/stage2-finish.sh
```

> Run it **as `visko`, not with `sudo`** — it self-elevates where needed. Running
> the app install as root makes `venv/`/`instance/` root-owned and the
> `User=visko` services fail to open the DB (the footgun spec §7c calls out).

It is **idempotent** — re-run it any time; each step checks before it changes.

---

## What runs where (so nothing is done twice)

| Stage | Mechanism | Covers |
|---|---|---|
| 1 — installer | `autoinstall/preseed.cfg` | Debian minimal, `visko`, SSH key, **broad** bootstrap sudoers, partition the OS disk, `openssh-server git curl ca-certificates build-essential python3-venv python3-dev pkg-config sudo` |
| 1 — first boot | `bootstrap/firstboot.sh` (one-shot) | Node 22, Claude Code, `git clone` of this repo, MOTD |
| **2b — finisher** | **`stage2-finish.sh`** (this page) | **§4 full package set, §5 BlueZ `--experimental`, §6/§7 venv + mosquitto + `ha-*` services via `install.sh`, persistent journald, verification** |
| 2b — by hand | you (printed at the end) | static-IP cutover, console password + narrow sudoers, sneakernet `devices.yaml`/`weather.env`, reboot test |

`stage2-finish.sh` calls `install.sh` for the app layer, so the two never conflict:
`install.sh` stays the portable app installer (also used on .245); the finisher adds
the **host-level** Stage-2 config around it.

---

## What `stage2-finish.sh` does NOT do, and why

These need a human, drop your SSH session, or involve PII — the script prints them
with exact commands every run:

1. **Static-IP cutover** — drops SSH; box-specific. This box uses `ifupdown`
   (`/etc/network/interfaces`); edit the address there and `sudo reboot`.
   `ha-dev`'s chosen address is **192.168.0.210**.
2. **Console password + narrow sudoers** — `sudo passwd visko`, then re-run with
   `--narrow-sudoers` (the script refuses if no password is set, so you can't lock
   yourself out of sudo by removing the NOPASSWD-ALL grant).
3. **Real device registry** — `instance/devices.yaml` (+ `instance/weather.env`) is
   PII, sneakernet-only, never in git. Until it's copied in, meters publish to
   `home/unknown/<mac>/raw`.
4. **Reboot test** — `sudo reboot`, confirm all `ha-*` services + the scanner return.

Optional, the script can do #2's sudoers half once a password exists:
```bash
sudo passwd visko
./provisioning/stage2-finish.sh --narrow-sudoers
```

---

## Hardware notes carried over from ha-dev

- **BLE radio:** the spec's intended radio is the **TP-Link UB500 (RTL8761B)** USB
  dongle. `ha-dev` had no dongle fitted and the **onboard MediaTek MT7922 BT worked**
  for passive `or_patterns` scanning — but it's the known-risk radio. The script
  detects which radio is present and warns if you're on the onboard one; fit the
  UB500 if the scanner shows watchdog restarts/stalls.
- **Storage:** `ha-dev` had a **single NVMe**, so the dual-disk split/mirror (spec §3)
  was skipped and `instance/db/` stays on the OS disk. A box with a real second NVMe
  should still do §3 by hand before running the finisher.
- **Python:** Debian 13 ships **3.13**; `requirements.txt` is already bumped to the
  cp313 wheels (`pyarrow 18.x`), so the venv build Just Works.

---

## Drive with Claude instead (on-device LLM directive)

If you'd rather supervise interactively — or you hit hardware that differs from the
notes above — run Claude on the box and hand it this prompt:

> You're on a freshly bootstrapped GMKtec G11 (Debian 13), repo at `~/home_automation`.
> Stage 1 (preseed + firstboot.sh) is done. Finish Stage 2: **first read
> `provisioning/02-full-server-spec.md` end-to-end**, then run
> `./provisioning/stage2-finish.sh` for the automatable §4–§7 work and handle the
> printed manual steps with me. This box is **DEV/validation**, not the production
> dictator — do **not** start `ha-controller`, do **not** disrupt or publish onto the
> live server at **192.168.0.245**, and **stop and ask** before anything that looks
> like a cutover. Flag (don't invent) any spec step that assumes hardware this box
> lacks (e.g. a second NVMe or the UB500 dongle).

`02-full-server-spec.md` remains the full reference and records on-device findings;
this page is just the fast, scripted path through it.
