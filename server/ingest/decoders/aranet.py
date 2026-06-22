"""
Aranet BLE advertisement decoder.

CORRECTED 2026-06-21: Aranet broadcasts its readings in **manufacturer data, company 0x0702**
(SAF Tehnika) via **extended advertising** — NOT service-data `fce0` (the earlier guess). Verified
live against an `AranetRn+` on a BT5 adapter and cross-checked byte-for-byte with the reference
`aranet4` library.

Supported: Aranet Radon Plus (type byte 0x03). The CO2 Aranet4 uses a different field layout (TODO if
one is ever added). Requires "Smart Home Integration" mode = On (the device then broadcasts).

Manufacturer-data layout (little-endian; readings block starts at offset 8):
  [0]      device type        uint8    (3 = Aranet Radon)
  [8:10]   radon              uint16   Bq/m³   (0xFFFF = warming up / no reading)
  [10:12]  temperature        uint16   raw/20 = °C
  [12:14]  pressure           uint16   raw/10 = hPa
  [14:16]  humidity           uint16   raw/10 = %
  [17]     battery            uint8    %
  [18]     status             uint8    (1 green / 2 amber / 3 red)
  [19:21]  interval           uint16   s between readings
  [21:23]  ago                uint16   s since the last reading was taken
  [23]     counter            uint8
"""
import logging

log = logging.getLogger(__name__)

COMPANY_ID: int = 0x0702
_TYPE_RADON: int = 0x03
_U16_NO_READING: int = 0xFFFF


def is_aranet(mfr_data: dict[int, bytes]) -> bool:
    return COMPANY_ID in (mfr_data or {})


def _u16(b: bytes, i: int) -> int:
    return b[i] | (b[i + 1] << 8)


def decode_manufacturer(mac: str, mfr_data: dict[int, bytes], rssi: int) -> dict | None:
    """Return {device_type, metrics, meta} or None. `mfr_data` is {company_id: bytes} (as bleak gives)."""
    raw = (mfr_data or {}).get(COMPANY_ID)
    if not raw or len(raw) < 24:
        if raw is not None:
            log.warning("aranet mfr data too short mac=%s len=%d", mac, len(raw))
        return None
    if raw[0] != _TYPE_RADON:
        log.debug("aranet non-radon type=0x%02x mac=%s — unsupported layout", raw[0], mac)
        return None

    radon = _u16(raw, 8)
    temperature = round(_u16(raw, 10) / 20.0, 2)
    pressure = round(_u16(raw, 12) / 10.0, 1)
    humidity = round(_u16(raw, 14) / 10.0, 1)
    battery = raw[17]
    status = raw[18]
    interval = _u16(raw, 19)
    ago = _u16(raw, 21)

    if not (-40.0 <= temperature <= 85.0):
        log.warning("aranet temp %s°C out of range mac=%s raw=%s", temperature, mac, raw.hex())
        return None

    metrics: dict = {
        "temperature_c": temperature,
        "pressure_hpa": pressure,
        "humidity_pct": humidity,
        "battery_pct": int(battery),
    }
    if radon != _U16_NO_READING:
        metrics["radon_bqm3"] = int(radon)        # device still warming up → omitted

    return {
        "device_type": "aranet_radon_plus",
        "metrics": metrics,
        # status: 1 green / 2 amber / 3 red; ago_s lets the relay back-date the reading's ts.
        "meta": {"status": int(status), "interval_s": int(interval), "ago_s": int(ago)},
    }
