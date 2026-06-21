# Stage 1 — Bootstrap ISO

**Goal:** boot the G11 once and end up with an SSH-reachable Debian box that has **git, Node, and
Claude Code** installed and this repo cloned — so an LLM can drive Stage 2 on-device.

This is deliberately minimal. It does *not* configure drivers, storage, BlueZ, the app, or services —
that's Stage 2 (`02-full-server-spec.md`), run by the on-device LLM.

---

## What the ISO does (unattended)

1. Installs **Debian 13 minimal** to the **first/smaller NVMe** (second NVMe left for data).
2. Creates user `visko`, key-only SSH, temporary broad `NOPASSWD` sudo (narrowed in Stage 2 §7).
3. Installs base toolchain: `openssh-server git curl ca-certificates build-essential python3-venv python3-dev pkg-config sudo`.
4. On **first boot** (online), `firstboot.sh` runs once and installs **Node LTS + Claude Code**, clones
   the repo to `~/home_automation`, writes an MOTD with next steps, then disables itself.

After it reboots, you SSH in and either run `claude` on-box or attach via **VSCode Remote-SSH** and
run Claude from your workstation against the server.

---

## Build it (on your workstation, not the target)

```bash
# 0. Tools
sudo apt install xorriso isolinux

# 1. Get the official Debian 13 netinst ISO (online, one-time)
#    https://www.debian.org/distrib/netinst   ->  debian-13.x.0-amd64-netinst.iso

# 2. Fill the three placeholders in the preseed:
#      - hostname (e.g. ha-prime)
#      - password hash:   mkpasswd -m sha-512        (from the `whois` package)
#      - SSH public key:  cat ~/.ssh/id_ed25519.pub
$EDITOR provisioning/autoinstall/preseed.cfg
grep -n PLACEHOLDER provisioning/autoinstall/preseed.cfg   # must print nothing

# 3. Also set the repo URL in the first-boot provisioner if your remote differs:
$EDITOR provisioning/bootstrap/firstboot.sh                # REPO_URL=...

# 4. Build the unattended ISO
provisioning/bootstrap/build-seed-iso.sh \
    debian-13.x.0-amd64-netinst.iso  ha-bootstrap.iso

# 5. Flash to USB (find the right /dev/sdX with `lsblk` — this ERASES it)
sudo dd if=ha-bootstrap.iso of=/dev/sdX bs=4M status=progress conv=fsync
```

## Boot the G11

1. Insert USB, power on, open the BIOS/boot menu (usually `Del`/`F7` on GMKtec), boot the USB.
2. In BIOS, confirm: **UEFI** boot, Secure Boot **off** (simplest for custom kernels later),
   and that wired LAN is enabled. The install is hands-off from here.
3. It installs, reboots, runs `firstboot.sh` (watch `/var/log/ha-firstboot.log`), and lands at a login.

## Verify Stage 1

```bash
ssh visko@<dhcp-ip-of-g11>
cat /var/log/ha-firstboot.log     # should end with "DONE."
node --version                    # >= 18
claude --version                  # Claude Code present
ls ~/home_automation              # repo cloned
```

If the repo is **private**, the clone step warns and skips — provide a GitHub PAT or deploy key, then:
`git clone https://<token>@github.com/heisenman/home-automation.git ~/home_automation`.

## Authenticate the LLM, then start Stage 2

```bash
cd ~/home_automation
claude            # complete OAuth login (online) or: export ANTHROPIC_API_KEY=...
```
Point it at **`provisioning/02-full-server-spec.md`** and let it proceed.

---

### Notes / gotchas
- **Bootstrap is online.** This is the first unit's fast path. For an air-gapped rebuild later, the
  same preseed points at a **local snapshot mirror** instead of `deb.debian.org`, and `firstboot.sh`
  installs Node/Claude from sneakernet artifacts (see `03-sneakernet-updates.md`).
- **EFI image path** in `build-seed-iso.sh` (`boot/grub/efi.img`) matches current Debian netinst; if a
  future ISO layout differs, adjust the `-e` argument.
- The broad bootstrap sudoers is a **temporary** convenience for unattended Stage 2; Stage 2 §7
  replaces it with the narrow `ha-services` rule.
