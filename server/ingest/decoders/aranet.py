"""
Aranet BLE advertisement decoder.

Protocol reference: aranet4 Python library (Anrijs/aranet4 on GitHub) and
Aranet Smart Home Integration broadcast specification.

Supported devices:
  Aranet4           — CO2 + temp + pressure + humidity + battery
  Aranet Radon Plus — above + radon (Bq/m³)

Detection: service data UUID 0000fce0-0000-1000-8000-00805f9b34fb.

Requires "Smart Home Integration" mode enabled on the device
(Settings → Smart Home Integration → On).
"""

import logging
import struct

log = logging.getLogger(__name__)

SERVICE_UUID: str = "0000fce0-0000-1000-8000-00805f9b34fb"

# Sentinel value meaning "no radon reading yet" (device warming up)
_RADON_NO_READING: int = 0xFFFF


def is_aranet(svc_data: dict) -> bool:
    return SERVICE_UUID in svc_data


def decode(
    mac: str,
    svc_data: dict[str, bytes],
    rssi: int,
) -> dict | None:
    """
    Return decoded metrics dict or None.

    Smart Home Integration advertisement service data layout (little-endian):
      Bytes  0–1   CO2            uint16   ppm
      Bytes  2–3   Temperature    uint16   raw / 20 = °C
      Bytes  4–5   Pressure       uint16   raw / 10 = hPa
      Byte   6     Humidity       uint8    %
      Byte   7     Battery        uint8    %
      Byte   8     Status flags   uint8
      Bytes  9–10  Interval       uint16   seconds between readings
      Byte   11    Age            uint8    seconds since last reading
      Bytes 12–13  Radon          uint16   Bq/m³  (Radon Plus only; 0xFFFF = no reading)
    """
    raw = svc_data.get(SERVICE_UUID, b"")
    log.debug("aranet raw mac=%s len=%d bytes=%s", mac, len(raw), raw.hex())

    if len(raw) < 12:
        log.warning("aranet advertisement too short mac=%s len=%d", mac, len(raw))
        return None

    try:
        co2, temp_raw, pressure_raw, humidity, battery = struct.unpack_from("<HHHBb", raw, 0)
    except struct.error as exc:
        log.warning("aranet struct unpack failed mac=%s: %s raw=%s", mac, exc, raw.hex())
        return None

    temperature = round(temp_raw / 20.0, 2)
    pressure = round(pressure_raw / 10.0, 1)

    # Reject implausible readings
    if not (300 <= co2 <= 10000):
        log.warning("aranet CO2 %s ppm out of range mac=%s raw=%s", co2, mac, raw.hex())
        return None
    if not (-40.0 <= temperature <= 85.0):
        log.warning("aranet temp %s°C out of range mac=%s raw=%s", temperature, mac, raw.hex())
        return None

    metrics: dict = {
        "co2_ppm": int(co2),
        "temperature_c": temperature,
        "pressure_hpa": pressure,
        "humidity_pct": int(humidity),
        "battery_pct": int(abs(battery)),  # pySwitchbot stores as signed; take abs
    }

    # Radon Plus: 14+ bytes
    if len(raw) >= 14:
        (radon,) = struct.unpack_from("<H", raw, 12)
        if radon != _RADON_NO_READING:
            metrics["radon_bqm3"] = int(radon)
        else:
            log.debug("aranet radon not yet available mac=%s", mac)

    return {
        "device_type": "aranet_radon_plus" if len(raw) >= 14 else "aranet4",
        "metrics": metrics,
    }
