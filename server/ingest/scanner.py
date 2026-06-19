"""
Passive BLE scanner — publishes SwitchBot and Aranet readings to MQTT.

Runs continuously; all SwitchBot and Aranet advertisements are decoded and
published as retained JSON messages on:
  home/<area>/<device_id>/state

Unknown SwitchBot and Aranet advertisements are published to:
  home/unknown/<mac>/raw
so they appear in the broker and can be inspected while the registry is being
populated.

Rate-limiting: a reading is re-published only if ≥ REPUBLISH_INTERVAL_S seconds
have passed OR a metric value has changed beyond CHANGE_THRESHOLD (by metric type).
This avoids flooding SQLite with identical rows every 2 s from 15 meters.

Usage:
  python3 scanner.py --registry instance/devices.yaml --broker localhost
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import yaml
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from decoders import aranet, switchbot

# ── Configuration ─────────────────────────────────────────────────────────────

REPUBLISH_INTERVAL_S: int = int(os.environ.get("HA_REPUBLISH_S", "60"))
BROKER_HOST: str = os.environ.get("HA_BROKER", "localhost")
BROKER_PORT: int = int(os.environ.get("HA_BROKER_PORT", "1883"))
SCANNING_MODE: str = os.environ.get("HA_SCAN_MODE", "passive")  # passive | active
MESSAGE_SCHEMA: int = 1

log = logging.getLogger("ha.scanner")


# ── Change thresholds (skip republish if change is noise) ─────────────────────

_CHANGE_THRESHOLDS: dict[str, float] = {
    "temperature_c": 0.1,
    "humidity_pct": 1.0,
    "battery_pct": 1.0,
    "co2_ppm": 5.0,
    "pressure_hpa": 0.2,
    "radon_bqm3": 1.0,
}


# ── Registry ──────────────────────────────────────────────────────────────────

def load_registry(path: Path) -> dict[str, dict]:
    """Return MAC → device-info dict. MACs normalised to uppercase."""
    if not path.exists():
        log.warning("Registry not found at %s — all devices will publish as unknown", path)
        return {}
    with path.open() as f:
        raw = yaml.safe_load(f) or {}
    return {mac.upper(): info for mac, info in raw.get("devices", {}).items()}


# ── MQTT ──────────────────────────────────────────────────────────────────────

def _build_mqtt_client() -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    return client


def _on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        log.info("MQTT connected to %s:%s", BROKER_HOST, BROKER_PORT)
    else:
        log.error("MQTT connect failed rc=%s", rc)


def _on_disconnect(client, userdata, disconnect_flags, rc, properties=None):
    if rc != 0:
        log.warning("MQTT unexpectedly disconnected rc=%s — will retry", rc)


def _mqtt_connect_with_retry(client: mqtt.Client) -> None:
    attempt = 0
    while True:
        try:
            client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
            client.loop_start()
            return
        except Exception as exc:
            attempt += 1
            wait = min(2 ** attempt, 60)
            log.warning("MQTT connect attempt %d failed: %s — retry in %ds", attempt, exc, wait)
            time.sleep(wait)


# ── State cache (dedup / rate-limit) ──────────────────────────────────────────

class _DeviceState:
    __slots__ = ("last_ts", "last_metrics")

    def __init__(self):
        self.last_ts: float = 0.0
        self.last_metrics: dict = {}

    def should_publish(self, metrics: dict) -> bool:
        now = time.monotonic()
        if now - self.last_ts >= REPUBLISH_INTERVAL_S:
            return True
        for key, value in metrics.items():
            threshold = _CHANGE_THRESHOLDS.get(key, 0.0)
            prev = self.last_metrics.get(key)
            if prev is None or abs(value - prev) >= threshold:
                return True
        return False

    def update(self, metrics: dict) -> None:
        self.last_ts = time.monotonic()
        self.last_metrics = dict(metrics)


# ── Core scanner ──────────────────────────────────────────────────────────────

class Scanner:
    def __init__(self, registry: dict, mqtt_client: mqtt.Client):
        self._registry = registry
        self._mqtt = mqtt_client
        self._state: dict[str, _DeviceState] = {}

    def _device_state(self, mac: str) -> _DeviceState:
        if mac not in self._state:
            self._state[mac] = _DeviceState()
        return self._state[mac]

    def _advertisement_callback(self, device: BLEDevice, adv: AdvertisementData) -> None:
        mac = device.address.upper()
        mfr = adv.manufacturer_data or {}
        svc = adv.service_data or {}
        rssi = adv.rssi if adv.rssi is not None else 0

        if switchbot.is_switchbot(mfr, svc):
            result = switchbot.decode(mac, mfr, svc, rssi)
            if result:
                self._publish(mac, result["device_type"], result["metrics"], rssi, "ble-adv")
            else:
                self._publish_raw(mac, "switchbot", mfr, svc, rssi)

        elif aranet.is_aranet(svc):
            result = aranet.decode(mac, svc, rssi)
            if result:
                self._publish(mac, result["device_type"], result["metrics"], rssi, "ble-adv")
            else:
                self._publish_raw(mac, "aranet", mfr, svc, rssi)

    def _publish(
        self,
        mac: str,
        device_type: str,
        metrics: dict,
        rssi: int,
        transport: str,
    ) -> None:
        state = self._device_state(mac)
        if not state.should_publish(metrics):
            return
        state.update(metrics)

        reg = self._registry.get(mac, {})
        device_id = reg.get("device_id") or f"unknown_{mac.replace(':', '').lower()}"
        area = reg.get("area", "unknown")
        if reg.get("device_type"):
            device_type = reg["device_type"]

        topic = f"home/{area}/{device_id}/state"
        payload = {
            "schema": MESSAGE_SCHEMA,
            "device_id": device_id,
            "device_type": device_type,
            "area": area,
            "ts": _utc_now(),
            "transport": transport,
            "metrics": metrics,
            "meta": {"rssi": rssi, "mac": mac},
        }
        self._mqtt.publish(topic, json.dumps(payload), qos=1, retain=True)
        log.debug("published %s %s", topic, metrics)

    def _publish_raw(
        self,
        mac: str,
        brand: str,
        mfr: dict,
        svc: dict,
        rssi: int,
    ) -> None:
        topic = f"home/unknown/{mac.replace(':', '').lower()}/raw"
        payload = {
            "brand": brand,
            "mac": mac,
            "ts": _utc_now(),
            "rssi": rssi,
            "manufacturer_data": {str(k): v.hex() for k, v in mfr.items()},
            "service_data": {k: v.hex() for k, v in svc.items()},
        }
        self._mqtt.publish(topic, json.dumps(payload), qos=0, retain=False)
        log.info("published raw (decode failed) %s", topic)

    async def run(self) -> None:
        log.info(
            "Starting BLE scanner mode=%s broker=%s:%s",
            SCANNING_MODE,
            BROKER_HOST,
            BROKER_PORT,
        )
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _sigterm(*_):
            log.info("SIGTERM received — shutting down")
            loop.call_soon_threadsafe(stop_event.set)

        signal.signal(signal.SIGTERM, _sigterm)
        signal.signal(signal.SIGINT, _sigterm)

        async with BleakScanner(
            detection_callback=self._advertisement_callback,
            scanning_mode=SCANNING_MODE,
        ):
            log.info("BLE scanner active")
            await stop_event.wait()

        log.info("Scanner stopped")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Entry point ───────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Home automation BLE scanner")
    p.add_argument("--registry", default="instance/devices.yaml", type=Path)
    p.add_argument("--broker", default=BROKER_HOST)
    p.add_argument("--broker-port", default=BROKER_PORT, type=int)
    p.add_argument("--scan-mode", default=SCANNING_MODE, choices=["passive", "active"])
    p.add_argument("--republish-interval", default=REPUBLISH_INTERVAL_S, type=int)
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stdout,
    )

    global BROKER_HOST, BROKER_PORT, SCANNING_MODE, REPUBLISH_INTERVAL_S
    BROKER_HOST = args.broker
    BROKER_PORT = args.broker_port
    SCANNING_MODE = args.scan_mode
    REPUBLISH_INTERVAL_S = args.republish_interval

    registry = load_registry(args.registry)
    log.info("Registry loaded: %d known devices", len(registry))

    client = _build_mqtt_client()
    _mqtt_connect_with_retry(client)

    scanner = Scanner(registry, client)
    asyncio.run(scanner.run())

    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
