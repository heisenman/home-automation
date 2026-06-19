"""
SwitchBot BLE advertisement decoder.

Protocol reference: SwitchBotAPI-BLE (openWonderLabs/SwitchBotAPI-BLE on GitHub)
and pySwitchbot adv_parsers. Implements our own decode; pySwitchbot not imported.

Supported devices:
  WoSensorTH      (Meter)       — model byte 0x54 'T'
  WoSensorTHPlus  (Meter Plus)  — model byte 0x69 'i'
  WoSensorTHPro   (Meter Pro)   — model byte 0x4F 'O' or 0x50 'P'

Detection: manufacturer data company-id 0x0969, OR service data UUID fd3d.
"""

import logging
import struct

log = logging.getLogger(__name__)

MANUFACTURER_ID: int = 0x0969
SERVICE_UUID: str = "0000fd3d-0000-1000-8000-00805f9b34fb"

# Known model bytes → human label
_MODEL_NAMES: dict[int, str] = {
    0x54: "switchbot_meter",
    0x69: "switchbot_meter_plus",
    0x4F: "switchbot_meter_pro",
    0x50: "switchbot_meter_pro",
}


def is_switchbot(mfr_data: dict, svc_data: dict) -> bool:
    return MANUFACTURER_ID in mfr_data or SERVICE_UUID in svc_data


def decode(
    mac: str,
    mfr_data: dict[int, bytes],
    svc_data: dict[str, bytes],
    rssi: int,
) -> dict | None:
    """
    Return decoded metrics dict or None if this advertisement cannot be parsed.
    Logs raw bytes at DEBUG level to aid protocol tuning on real hardware.
    """
    raw_mfr = mfr_data.get(MANUFACTURER_ID, b"")
    raw_svc = svc_data.get(SERVICE_UUID, b"")

    log.debug(
        "switchbot raw mac=%s mfr(%d)=%s svc(%d)=%s",
        mac,
        len(raw_mfr),
        raw_mfr.hex(),
        len(raw_svc),
        raw_svc.hex(),
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

    return {
        "device_type": device_type,
        "metrics": metrics,
    }


def _infer_device_type(raw_mfr: bytes, raw_svc: bytes) -> str:
    if raw_mfr and len(raw_mfr) >= 1:
        model_byte = raw_mfr[0]
        return _MODEL_NAMES.get(model_byte, f"switchbot_unknown_0x{model_byte:02x}")
    return "switchbot_meter"


def _decode_meter(svc: bytes, mfr: bytes) -> dict | None:
    """
    Service data layout (bytes after UUID, 0-indexed):
      [0]  model / sequence flags
      [1]  active flag | status bits
      [2]  battery %  (& 0x7F)
      [3]  temp decimal tenths  (& 0x0F → 0.0–0.9 °C)
      [4]  temp integer  (& 0x7F → °C); bit 7 = 1 means positive
      [5]  humidity %  (& 0x7F)

    Falls back to manufacturer data if service data absent/short.
    """
    if svc and len(svc) >= 6:
        return _parse_svc_bytes(svc)

    # Some Meter Pro firmware only broadcasts manufacturer data
    if mfr and len(mfr) >= 6:
        # Manufacturer data starts after the model byte; layout mirrors service data
        # bytes [1:] — empirically observed; may need tuning per firmware version
        return _parse_svc_bytes(mfr[1:])

    return None


def _parse_svc_bytes(data: bytes) -> dict | None:
    if len(data) < 6:
        return None
    try:
        battery = data[2] & 0x7F
        temp_frac = (data[3] & 0x0F) * 0.1
        temp_int = data[4] & 0x7F
        temp_positive = bool(data[4] & 0x80)
        temperature = temp_int + temp_frac
        if not temp_positive:
            temperature = -temperature
        humidity = data[5] & 0x7F

        # Sanity ranges; reject obviously garbled readings
        if not (-40.0 <= temperature <= 60.0):
            log.warning("switchbot temperature %s out of range — raw %s", temperature, data.hex())
            return None
        if not (0 <= humidity <= 100):
            log.warning("switchbot humidity %s out of range — raw %s", humidity, data.hex())
            return None

        return {
            "temperature_c": round(temperature, 1),
            "humidity_pct": int(humidity),
            "battery_pct": int(battery),
        }
    except (IndexError, struct.error) as exc:
        log.warning("switchbot parse error %s — raw %s", exc, data.hex())
        return None
