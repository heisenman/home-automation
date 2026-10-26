"""Venstar ColorTouch local API (ADR-0043) — a stdlib HTTP client plus the PURE translation both the control
driver and the telemetry bridge use. Source of truth: Venstar's published spec (developer.venstar.com/
documentation, API v4, fetched 2026-10-24; summarised in docs/design/venstar-thermostat-intake.md §2) and the
field-proven `venstarcolortouch` client (fan-only POST, HTTP digest auth). Every field/range here is a
HYPOTHESIS until verified live on the installed T7850 (intake §5) — Principle 5.

Wire facts used:
  GET  /query/info      mode 0 off|1 heat|2 cool|3 auto · state 0 idle|1 heating|2 cooling|3 lockout|4 error
                        fan 0 auto|1 on · fanstate 0|1 · tempunits 0 °F|1 °C · heattemp/cooltemp/spacetemp
                        heattempmin/max, cooltempmin/max, setpointdelta · schedule · away · hum · activestage
  POST /control         form-encoded; any call WITH mode must carry heattemp AND cooltemp (current tempunits);
                        auto needs cooltemp > heattemp by >= setpointdelta; `pin` if screen-locked.
                        Fan alone (`fan=1`) is a valid call. Reply {"success":true} | {"error":true,"reason":…}
                        — HTTP 200 either way, so the BODY is the verdict.
"""
from __future__ import annotations

import json
import ssl
import urllib.parse
import urllib.request

MODES = {0: "off", 1: "heat", 2: "cool", 3: "auto"}
STATES = {0: "idle", 1: "heating", 2: "cooling", 3: "lockout", 4: "error"}


class VenstarError(RuntimeError):
    """The thermostat refused a call, or answered with something the spec doesn't describe."""


def f_to_c(v):
    return None if v is None else round((float(v) - 32.0) * 5.0 / 9.0, 2)


def c_to_f(v):
    return None if v is None else round(float(v) * 9.0 / 5.0 + 32.0, 2)


def _units(info: dict) -> str:
    u = info.get("tempunits")
    if u not in (0, 1):
        raise VenstarError(f"tempunits {u!r} is not 0 (°F) or 1 (°C)")
    return "degF" if u == 0 else "degC"


def info_metrics(info: dict) -> tuple[dict, dict]:
    """(thermostat metrics, blower metrics) for the canonical state topic. Temperatures always land in °C
    (the house's canonical unit); the thermostat's own temperature/humidity are a device self-report —
    the bridge publishes them non-authoritative (CONFORMANCE A / ADR-0014 R4). /query/sensors (remote and
    outdoor sensors) is not used: none are fitted, and an unconnected Outdoor sensor reads 0."""
    conv = f_to_c if _units(info) == "degF" else (lambda v: None if v is None else float(v))
    t = {
        "temperature_c": conv(info.get("spacetemp")),
        "heat_setpoint_c": conv(info.get("heattemp")),
        "cool_setpoint_c": conv(info.get("cooltemp")),
        "hvac_mode": info.get("mode"),
        "hvac_state": info.get("state"),
        "active_stage": info.get("activestage"),
        "schedule_on": info.get("schedule"),
        "away": info.get("away"),
        "humidity_pct": info.get("hum") if isinstance(info.get("hum"), (int, float)) and info.get("hum") > 0
        else None,
    }
    b = {"running": info.get("fanstate"), "fan_on_setting": info.get("fan")}
    return ({k: v for k, v in t.items() if v is not None}, {k: v for k, v in b.items() if v is not None})


def plan_setpoint(info: dict, channel: str, value: float, unit: str) -> dict:
    """Form params that set ONE setpoint and keep the other — or VenstarError. `value` is in `unit`
    (the trait's configured unit); the thermostat's own tempunits must match it, never silently convert."""
    if channel not in ("heat", "cool"):
        raise VenstarError(f"unknown setpoint channel {channel!r}")
    if unit != _units(info):
        raise VenstarError(f"trait unit {unit} but the thermostat is set to {_units(info)} — fix one of them")
    heat, cool = float(info["heattemp"]), float(info["cooltemp"])
    if channel == "heat":
        heat = float(value)
        lo, hi = info.get("heattempmin"), info.get("heattempmax")
    else:
        cool = float(value)
        lo, hi = info.get("cooltempmin"), info.get("cooltempmax")
    if lo is not None and hi is not None and not lo <= float(value) <= hi:
        raise VenstarError(f"{channel} {value:g} outside the thermostat's own limits {lo}..{hi}")
    delta = float(info.get("setpointdelta") or 0)
    if cool - heat < delta:
        # never move the OTHER setpoint behind someone's back — refuse, and say what would be needed
        raise VenstarError(f"heat {heat:g} / cool {cool:g} closer than the thermostat's setpointdelta {delta:g}")
    return {"heattemp": _num(heat), "cooltemp": _num(cool)}


def plan_mode(info: dict, mode: int) -> dict:
    """Form params for a mode change: the spec requires heattemp + cooltemp on any call with mode."""
    if mode not in MODES:
        raise VenstarError(f"mode {mode!r} not one of {MODES}")
    avail = info.get("availablemodes", 0)
    allowed = {0: {0, 1, 2, 3}, 1: {0, 1, 2}, 2: {0, 1}, 3: {0, 2}}.get(avail, {0, 1, 2, 3})
    if mode not in allowed:
        raise VenstarError(f"mode {MODES[mode]} not available (availablemodes={avail})")
    return {"mode": mode, "heattemp": _num(info["heattemp"]), "cooltemp": _num(info["cooltemp"])}


def plan_fan(on: bool) -> dict:
    return {"fan": 1 if on else 0}


def _num(v):
    f = float(v)
    return int(f) if f == int(f) else round(f, 1)


class VenstarClient:
    """Thin HTTP client. `proto` http|https; with user+password it answers whichever challenge the unit sends:
    the T7850 manual calls the fields "Basic Auth User/Password", the HA client uses digest (intake §5 step 3).
    HTTPS on a ColorTouch is a self-signed cert on an air-gapped LAN: verification is off and that is a
    known, documented trade (ADR-0043) — the network, not the cert, is the boundary."""

    def __init__(self, host: str, *, proto: str = "http", user: str | None = None, password: str | None = None,
                 pin: str | None = None, timeout: float = 5.0, opener=None):
        self.base = f"{proto}://{host}"
        self.pin, self.timeout = pin, timeout
        if opener is None:
            handlers = []
            if user and password:
                pm = urllib.request.HTTPPasswordMgrWithDefaultRealm()
                pm.add_password(None, self.base, user, password)
                handlers += [urllib.request.HTTPDigestAuthHandler(pm), urllib.request.HTTPBasicAuthHandler(pm)]
            if proto == "https":
                ctx = ssl.create_default_context()
                ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
                handlers.append(urllib.request.HTTPSHandler(context=ctx))
            opener = urllib.request.build_opener(*handlers)
        self._open = opener.open

    def _json(self, path: str, data: dict | None = None) -> dict:
        body = None
        if data is not None:
            if self.pin:
                data = {**data, "pin": self.pin}
            body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(self.base + path, data=body, method="POST" if body else "GET",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"} if body else {})
        with self._open(req, timeout=self.timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except ValueError:
            raise VenstarError(f"{path}: not JSON ({raw[:80]!r})")

    def api(self) -> dict:
        return self._json("/")

    def info(self) -> dict:
        return self._json("/query/info")

    def sensors(self) -> dict:
        return self._json("/query/sensors")

    def control(self, params: dict) -> None:
        r = self._json("/control", params)
        if not r.get("success"):
            raise VenstarError(f"/control refused {params}: {r.get('reason') or r}")
