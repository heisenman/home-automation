"""Midea LAN device driver + issuer Transport (ADR-0011).

The dehumidifier is a WiFi appliance, not a BLE node — so instead of publishing a signed command to a
node over MQTT, its Transport translates the command's trait/action/args into a local
`midea-beautiful-air` call (authenticated by the saved token+key, fully on the LAN) and reports the
new state back to the issuer for closed-loop reconciliation. It's a trusted local driver, so the
command's HMAC isn't re-verified here (that's for untrusted nodes); the issuer already authorized it.

Trait → Midea mapping (MVP):
    switchable {on}     -> --running       , reported {"on": running}
    setpoint   {value}  -> --target-humidity, reported {"value": target}
    ranged     {level}  -> --fan-speed      , reported {"level": fan}

The CLI runner is injectable so the Transport is unit-testable with no hardware.
"""
from __future__ import annotations

import logging
import subprocess

from . import protocol

log = logging.getLogger("ha.control.midea")

_CLI = "venv/bin/midea-beautiful-air-cli"

# normalize the CLI's "  key = value" status lines into typed fields
_BOOL = {"true": True, "false": False, "on": True, "off": False}


def _parse_status(text: str) -> dict:
    raw = {}
    for line in text.splitlines():
        if line.startswith("  ") and "=" in line:
            k, _, v = line.partition("=")
            raw[k.strip()] = v.strip()
    out = {}

    def num(key, cast=int):
        if key in raw:
            try:
                out_key = cast(float(raw[key]))
                return out_key
            except ValueError:
                return None
        return None

    if "running" in raw:
        out["running"] = _BOOL.get(raw["running"].lower())
    if "online" in raw:
        out["online"] = _BOOL.get(raw["online"].lower())
    if "tank" in raw:
        out["tank_full"] = _BOOL.get(raw["tank"].lower())
    for cli_key, field in (("target%", "target"), ("humid%", "humidity"),
                           ("fan", "fan"), ("mode", "mode"), ("error", "error"), ("temp", "temp")):
        v = num(cli_key, float if cli_key == "temp" else int)
        if v is not None:
            out[field] = v
    return out


class MideaDriver:
    """Wraps the proven midea-beautiful-air CLI for one appliance. `runner` runs argv → stdout text
    (default subprocess); injectable for tests."""
    def __init__(self, ip: str, token: str, key: str, cli: str = _CLI, runner=None):
        self.ip, self.token, self.key, self.cli = ip, token, key, cli
        self._run = runner or self._subprocess

    def _subprocess(self, argv: list[str]) -> str:
        return subprocess.run(argv, capture_output=True, text=True, timeout=40).stdout

    def _argv(self, sub: str, *extra: str) -> list[str]:
        return [self.cli, sub, "--ip", self.ip, "--token", self.token, "--key", self.key, *extra]

    def status(self) -> dict:
        return _parse_status(self._run(self._argv("status")))

    def set(self, **flags) -> dict:
        extra: list[str] = []
        for k, v in flags.items():
            extra += [f"--{k.replace('_', '-')}", str(v)]
        return _parse_status(self._run(self._argv("set", *extra)))


# trait -> (midea set flag, status field carrying its value, reported state key)
_TRAIT_MAP = {
    "switchable": ("running", "running", "on"),
    "setpoint": ("target_humidity", "target", "value"),
    "ranged": ("fan_speed", "fan", "level"),
}


class MideaTransport:
    """issuer Transport for Midea LAN appliances. `drivers` maps our device_id -> MideaDriver."""
    def __init__(self, drivers: dict[str, MideaDriver]):
        self.drivers = drivers

    def send_and_wait(self, *, node, device_id, area, cmd, now=None, timeout=5.0):
        drv = self.drivers.get(device_id)
        if drv is None:
            return None                                  # not a Midea device → issuer maps to no-ack
        trait, action, args = cmd.get("trait"), cmd.get("action"), cmd.get("args", {})
        m = _TRAIT_MAP.get(trait)
        if m is None or action != "set":
            return protocol.build_ack(cmd_id=cmd["id"], status="rejected",
                                      reason=f"midea: unsupported {trait}/{action}")
        flag, status_field, report_key = m
        # the normalized arg value lives under the trait's own key (on|value|level)
        val = args.get("on" if trait == "switchable" else "value" if trait == "setpoint" else "level")
        try:
            state = drv.set(**{flag: val})
        except Exception as e:                           # transport/connection failure
            log.warning("midea set failed for %s: %s", device_id, e)
            return None
        reported = {report_key: state.get(status_field)}
        return protocol.build_ack(cmd_id=cmd["id"], status="ok", reported_state=reported,
                                  source="commanded")


def load_drivers_from_env(env: dict, device_id: str, cli: str = _CLI) -> dict:
    """Build {device_id: MideaDriver} from a parsed instance/midea-device.env (MIDEA_IP/TOKEN/KEY)."""
    ip, token, key = env.get("MIDEA_IP"), env.get("MIDEA_TOKEN"), env.get("MIDEA_KEY")
    if not (ip and token and key):
        return {}
    return {device_id: MideaDriver(ip, token, key, cli=cli)}
