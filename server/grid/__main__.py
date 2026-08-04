"""Voluntary load-shed runner — one pass, for a systemd timer on .210 (ADR-0037).

  venv/bin/python3 -m server.grid --once            # what the timer runs
  venv/bin/python3 -m server.grid --once --dry-run  # decide + log, touch nothing

Reads a shed window from the configured source, then for each configured device applies the pure
decision law in `shed.py` against the device's own authoritative humidity reading. Config comes from
`instance/grid-shed.env` (see config-examples/grid-shed.env.example).

Fail-safe posture, deliberately: **every failure path ends in "do not shed".** No source, no signal, no
network, no guardrail reading, a bad config — all of them mean the devices keep running their normal
automation. The only thing this lane can do on a bad day is nothing.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.control.secret_store import api_token, available_master   # noqa: E402
from server.grid import shed as law                                    # noqa: E402
from server.grid.client import Ha2Client                               # noqa: E402
from server.grid.sources import build_source                           # noqa: E402

log = logging.getLogger("ha.grid")


def _state_path(p: str | None) -> Path:
    return Path(p or "instance/grid-shed.state.json")


def parse_guardrails(spec: str) -> dict[str, float | None]:
    """'dehumidifier_living_room:55,purifier_living_room:35' -> {device: ceiling}.

    A ceiling of `none` opts a device out of the guardrail entirely (it then sheds on the window alone).
    Every device MUST appear: an unlisted device is a config error raised at startup, not a surprise at
    5pm. The ceiling is read in that device's OWN control metric — %RH for the dehumidifier,
    ug/m3 for the purifier — so the numbers are not comparable to each other and must not be defaulted."""
    out: dict[str, float | None] = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise SystemExit(f"bad guardrail {part!r} — want '<device_id>:<ceiling|none>'")
        dev, _, val = part.partition(":")
        dev, val = dev.strip(), val.strip().lower()
        out[dev] = None if val in ("none", "off", "-") else float(val)
    return out


def load_state(path: Path) -> dict:
    """{device_id: expiry} for the shed THIS lane believes it set. Kept on disk so a restart between
    runs does not forget which overrides are ours and start trampling a human's pause."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(path)                       # atomic: a crash mid-write can't leave a truncated state


def run_once(client, source, devices, ceilings, max_age_s, state_path, now=None, dry_run=False,
             tz=None) -> int:
    now = now or time.time()
    state = load_state(state_path)
    window = source.window(now)
    if window is None:
        log.info("source=%s: no active shed window", source.name)
    else:
        # Log the window in the UTILITY's zone, not the box's. .210 runs on UTC, and a log line reading
        # "window 00:00 -> 04:00" for a 5-9pm peak is exactly how someone later concludes it is broken.
        zone = ZoneInfo(tz or "America/Los_Angeles")
        fmt = lambda t: datetime.fromtimestamp(t, zone).strftime("%H:%M %Z")   # noqa: E731
        log.info("source=%s: window %s -> %s (%s)", source.name,
                 fmt(window.start), fmt(window.end), window.reason)

    changed = 0
    for device_id in devices:
        value, why = client.guardrail(device_id, max_age_s)
        live = client.live_override_expiry(device_id)
        ours = state.get(device_id)
        d = law.decide(now, window, value, ceilings.get(device_id), live, ours)
        log.info("%s | %s | %s | %s", device_id, why, d.action.upper(), d.reason)
        if dry_run:
            continue
        try:
            if d.action == law.SHED:
                exp = client.shed(device_id, d.duration_min)
                if exp is not None:
                    state[device_id] = exp
                    changed += 1
            elif d.action == law.RELEASE:
                if live is not None:
                    client.release(device_id)
                state.pop(device_id, None)
                changed += 1
        except Exception as e:              # one bad device must not stop the others
            log.error("%s: %s failed: %s", device_id, d.action, e)
    if not dry_run:
        save_state(state_path, state)
    return changed


def main() -> None:
    ap = argparse.ArgumentParser(description="Voluntary grid load-shed (ADR-0037)")
    ap.add_argument("--source", default=os.environ.get("HA_GRID_SOURCE"),
                    help="schedule | nws  (imap/manual declared, not yet implemented)")
    ap.add_argument("--devices", default=os.environ.get("HA_GRID_DEVICES", ""),
                    help="comma-separated control device_ids to curtail")
    ap.add_argument("--guardrails", default=os.environ.get("HA_GRID_GUARDRAILS", ""),
                    help="per-device ceiling in that device's OWN control metric, e.g. "
                         "'dehumidifier_living_room:55,purifier_living_room:35' ('none' to opt out)")
    ap.add_argument("--window", default=os.environ.get("HA_GRID_WINDOW", "17:00-21:00"))
    ap.add_argument("--season", default=os.environ.get("HA_GRID_SEASON", "06-01..09-30"))
    # The UTILITY's wall clock, not the box's — .210 runs on UTC, so a "17:00-21:00" peak window read as
    # box-local would silently shed the wrong four hours (10:00-14:00 Pacific) every single day.
    ap.add_argument("--tz", default=os.environ.get("HA_GRID_TZ", "America/Los_Angeles"))
    ap.add_argument("--lat", default=os.environ.get("HA_GRID_LAT",
                                                    os.environ.get("HA_WEATHER_LAT")))
    ap.add_argument("--lon", default=os.environ.get("HA_GRID_LON",
                                                    os.environ.get("HA_WEATHER_LON")))
    ap.add_argument("--api", default=os.environ.get("HA_GRID_API", "http://192.168.1.200:8123"))
    ap.add_argument("--max-sensor-age", type=float,
                    default=float(os.environ.get("HA_GRID_MAX_SENSOR_AGE_S", "900")))
    ap.add_argument("--state", default=os.environ.get("HA_GRID_STATE"))
    ap.add_argument("--once", action="store_true", help="one pass and exit (what the timer runs)")
    ap.add_argument("--dry-run", action="store_true", help="decide and log, change nothing")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")

    if not args.source:
        raise SystemExit("HA_GRID_SOURCE is unset — the lane is installed but not armed. "
                         "Set it in instance/grid-shed.env (see config-examples/grid-shed.env.example).")
    devices = [d.strip() for d in args.devices.split(",") if d.strip()]
    if not devices:
        raise SystemExit("HA_GRID_DEVICES is empty — nothing to curtail.")
    ceilings = parse_guardrails(args.guardrails)
    missing = [d for d in devices if d not in ceilings]
    if missing:
        raise SystemExit(f"no guardrail configured for {', '.join(missing)} — set HA_GRID_GUARDRAILS "
                         "(use '<device>:none' to shed it with no guardrail). Refusing to curtail a "
                         "device whose safe limit nobody has stated.")

    master = available_master()
    if not master:
        raise SystemExit("no master passphrase on this box — cannot derive the ha-2 admin bearer.")

    source = build_source(args.source, lat=args.lat, lon=args.lon,
                          window_spec=args.window, season=args.season, tz=args.tz)
    client = Ha2Client(base=args.api, token=api_token(master))
    n = run_once(client, source, devices, ceilings, args.max_sensor_age,
                 _state_path(args.state), dry_run=args.dry_run, tz=args.tz)
    log.info("pass complete: %d change(s)%s", n, " (dry-run)" if args.dry_run else "")


if __name__ == "__main__":
    main()
