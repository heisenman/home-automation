# provisioning/ — device & box recipes

How to stand up each box/device from scratch. Reproducible, gated where it touches live/critical hardware.

## Map

| Path | What |
|------|------|
| `01-bootstrap-iso.md` … `04-post-install.md`, `bootstrap/`, `autoinstall/`, `stage2-finish.sh` | Full server install (dictator/standby box) |
| `reterminal/` | Seeed reTerminal **panels** (D1001/E1001): factory-flash backup, beachhead firmware, BLE edge node, **C6 serial-flash** (ADR-0019) |
| `levoit/` | Levoit air purifier → local ESPHome (cloud severed) |
| `openwrt/` | Air-gapped router (R7800) prestage |
| `ntfy/` | Self-hosted push (MQTT → ntfy → phone) |
| `broker-auth-cutover.md`, `control-go-live.md` | Broker auth posture; control-loop go-live |
| `03-sneakernet-updates.md` | Offline update path (air-gapped) |

## Contracts

- **Image before you overwrite.** Back up factory/OEM firmware **off-git** before reflashing anything (Levoit
  OEM, reTerminal P4 + C6 factory images). This has saved us repeatedly.
- **Beachhead-first flashing:** prove WiFi + OTA on a minimal build before adding peripheral drivers you can't
  easily re-open (sealed devices).
- New devices should compose from the shared firmware catalog (ADR-0020) + a thin platform shim, and get a
  column in [../edge/MATRIX.md](../edge/MATRIX.md).
- See per-subdir `README.md` for the device-specific recipe; runbook index in [../SKILLS.md](../SKILLS.md).
