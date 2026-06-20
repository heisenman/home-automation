"""
SwitchBot BLE advertisement decoder.

Protocol reference: SwitchBotAPI-BLE (openWonderLabs/SwitchBotAPI-BLE on GitHub)
and pySwitchbot adv_parsers. Implements our own decode; pySwitchbot not imported.

Two wire formats observed on real hardware:

  Format A — service data (UUID fd3d, ≥6 bytes):
    [0]  model byte  (& 0x7F → model ID)
    [1]  status flags
    [2]  battery %   (& 0x7F)
    [3]  temp tenths (& 0x0F → 0.0–0.9 °C)
    [4]  temp integer (& 0x7F); bit 7 = 1 means positive
    [5]  humidity %  (& 0x7F)
  Used by: WoSensorTH (Meter, 'T'), WoSensorTHPlus (Meter Plus, 'i'),
           WoSensorTHPro (Meter Pro, 'O')

  Format B — manufacturer data (company 0x0969, ≥12 bytes):
    [0–5]  device MAC address (6 bytes, same order as BLE address)
    [6]    battery %   (& 0x7F)
    [7]    sequence / unknown
    [8]    temp tenths (& 0x0F → 0.0–0.9 °C)
    [9]    temp integer (& 0x7F); bit 7 = 1 means positive
    [10]   humidity %  (& 0x7F)
    [11]   unknown
  Used by: WoSensorTHO (Outdoor Meter, 'w') and newer firmware variants.
  Service data present but only 3 bytes (model byte + 2 status bytes).

Detection: manufacturer data company-id 0x0969, OR service data UUID fd3d.
"""

import logging
import struct

log = logging.getLogger(__name__)

MANUFACTURER_ID: int = 0x0969
SERVICE_UUID: str = "0000fd3d-0000-1000-8000-00805f9b34fb"

# Model byte (svc[0] & 0x7F) → device_type label
_MODEL_NAMES: dict[int, str] = {
    0x54: "switchbot_meter",           # 'T' WoSensorTH
    0x69: "switchbot_meter_plus",      # 'i' WoSensorTHPlus
    0x4F: "switchbot_meter_pro",       # 'O' WoSensorTHPro
    0x50: "switchbot_meter_pro",       # 'P' WoSensorTHPro alt
    0x77: "switchbot_meter_outdoor",   # 'w' WoSensorTHO
}


def is_switchbot(mfr_data: dict, svc_data: dict) -> bool:
    return MANUFACTURER_ID in mfr_data or SERVICE_UUID in svc_data


def decode(
    mac: str,
    mfr_data: dict[int, bytes],
    svc_data: dict[str, bytes],
    rssi: int,
) -> dict | None:
    """Return decoded metrics dict or None if this advertisement cannot be parsed."""
    raw_mfr = mfr_data.get(MANUFACTURER_ID, b"")
    raw_svc = svc_data.get(SERVICE_UUID, b"")

    log.debug(
        "switchbot raw mac=%s mfr(%d)=%s svc(%d)=%s",
        mac, len(raw_mfr), raw_mfr.hex(), len(raw_svc), raw_svc.hex(),
    )

    device_type = _infer_device_type(raw_mfr, raw_svc)
    metrics = _decode_meter(raw_svc, raw_mfr)

    if metrics is None:
        log.warning(
            "switchbot decode failed mac=%s — raw mfr=%s svc=%s",
            mac,
            raw_mfr.hex() if raw_mfr else "none",
            raw_svc.hex() if raw_svc else "none",
        )
        return None

    return {"device_type": device_type, "metrics": metrics}


def _infer_device_type(raw_mfr: bytes, raw_svc: bytes) -> str:
    # Service data byte 0 carries the model byte for all known variants
    if raw_svc and len(raw_svc) >= 1:
        model_byte = raw_svc[0] & 0x7F
        return _MODEL_NAMES.get(model_byte, f"switchbot_unknown_0x{model_byte:02x}")
    return "switchbot_meter"


def _decode_meter(svc: bytes, mfr: bytes) -> dict | None:
    # Format A: service data ≥6 bytes (Meter, Meter Plus, Meter Pro)
    if svc and len(svc) >= 6:
        return _parse_svc_bytes(svc)

    # Format B: manufacturer data with 6-byte MAC prefix (Outdoor Meter, newer firmware)
    if mfr and len(mfr) >= 11:
        return _parse_mfr_bytes(mfr[6:])

    return None


def _parse_svc_bytes(data: bytes) -> dict | None:
    """Parse Format A service data (model/flags/battery/temp/humidity)."""
    if len(data) < 6:
        return None
    try:
        battery    = data[2] & 0x7F
        temp_frac  = (data[3] & 0x0F) * 0.1
        temp_int   = data[4] & 0x7F
        positive   = bool(data[4] & 0x80)
        temperature = temp_int + temp_frac
        if not positive:
            temperature = -temperature
        humidity = data[5] & 0x7F
        return _validated(temperature, humidity, battery, data.hex())
    except (IndexError, struct.error) as exc:
        log.warning("switchbot svc parse error %s — raw %s", exc, data.hex())
        return None


def _parse_mfr_bytes(data: bytes) -> dict | None:
    """Parse Format B payload after stripping the 6-byte MAC prefix."""
    if len(data) < 5:
        return None
    try:
        battery    = data[0] & 0x7F   # mfr[6]
        # data[1] = sequence/unknown   # mfr[7]
        temp_frac  = (data[2] & 0x0F) * 0.1  # mfr[8]
        temp_int   = data[3] & 0x7F           # mfr[9]
        positive   = bool(data[3] & 0x80)
        temperature = temp_int + temp_frac
        if not positive:
            temperature = -temperature
        humidity = data[4] & 0x7F             # mfr[10]
        return _validated(temperature, humidity, battery, data.hex())
    except (IndexError, struct.error) as exc:
        log.warning("switchbot mfr parse error %s — raw %s", exc, data.hex())
        return None


def _validated(temperature: float, humidity: int, battery: int, raw_hex: str) -> dict | None:
    if not (-40.0 <= temperature <= 60.0):
        log.warning("switchbot temperature %s out of range — raw %s", temperature, raw_hex)
        return None
    if not (0 <= humidity <= 100):
        log.warning("switchbot humidity %s out of range — raw %s", humidity, raw_hex)
        return None
    return {
        "temperature_c": round(temperature, 1),
        "humidity_pct": int(humidity),
        "battery_pct": int(battery),
    }
