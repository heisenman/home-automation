#!/usr/bin/env python3
"""vt — one-shot Venstar commands for INTERACTIVE install testing (ADR-0043 intake §5). A fixed lookup table:
each verb is one POST (or none), followed by an immediate readback line, so an operator at the furnace gets
an answer in about a second.

    venv/bin/python tools/vt.py s                status line
    venv/bin/python tools/vt.py w [SECS]         status every 2 s (default 60)
    venv/bin/python tools/vt.py <verb>           send + readback; verbs below

Talks straight to the thermostat (VENSTAR_HOST, default 192.168.1.118) with the same client the driver uses.
Not a control path: nothing here goes through the issuer — it is a bench tool, like a multimeter.
"""
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.control.venstar_api import VenstarClient, VenstarError  # noqa: E402

MODES = {0: "OFF", 1: "HEAT", 2: "COOL", 3: "AUTO"}
STATES = {0: "idle", 1: "HEATING", 2: "COOLING", 3: "lockout", 4: "error"}


def table(i):
    """verb -> POST params. Mode calls carry both setpoints (spec); `None` setpoint = keep the current one."""
    h, c = i["heattemp"], i["cooltemp"]
    return {
        "fan_on":   {"fan": 1},
        "fan_auto": {"fan": 0},
        "heat78":   {"mode": 1, "heattemp": 78, "cooltemp": 82},     # call for heat (house ~72-75)
        "heat_sp70": {"heattemp": 70, "cooltemp": c},                 # end a heat call by setpoint
        "cool70":   {"mode": 2, "heattemp": 66, "cooltemp": 70},     # call for cooling
        "cool_sp80": {"heattemp": h, "cooltemp": 80},                 # end a cool call by setpoint
        "off":      {"mode": 0, "heattemp": h, "cooltemp": c},        # mode Off, setpoints unchanged
        "off66":    {"mode": 0, "heattemp": 66, "cooltemp": 70},      # mode Off, back to the 66/70 baseline
        "restore":  {"mode": 0, "heattemp": 71, "cooltemp": 79, "fan": 0},
    }


def line(i):
    return (f"{datetime.now(ZoneInfo('America/Los_Angeles')):%H:%M:%S %Z}  mode {MODES.get(i['mode'], i['mode'])}  "
            f"state {STATES.get(i['state'], i['state'])} (stage {i.get('activestage')})  "
            f"fan {'ON' if i['fan'] else 'auto'} / blower {'RUNNING' if i['fanstate'] else 'off'}  "
            f"room {i['spacetemp']:g}  heat {i['heattemp']:g}  cool {i['cooltemp']:g}")


def main():
    c = VenstarClient(os.environ.get("VENSTAR_HOST", "192.168.1.118"), timeout=3)
    verb = sys.argv[1] if len(sys.argv) > 1 else "s"
    i = c.info()
    if verb == "s":
        print(line(i))
        return
    if verb == "w":
        end = time.time() + float(sys.argv[2] if len(sys.argv) > 2 else 60)
        while True:
            print(line(i), flush=True)
            if time.time() >= end:
                return
            time.sleep(2)
            i = c.info()
    t = table(i)
    if verb not in t:
        sys.exit(f"unknown verb {verb!r}; one of: s w {' '.join(t)}")
    try:
        c.control(t[verb])
        print(f"{verb}: sent {t[verb]} -> accepted")
    except VenstarError as e:
        print(f"{verb}: sent {t[verb]} -> REFUSED: {e}")
    time.sleep(0.5)
    print(line(c.info()))


if __name__ == "__main__":
    main()
