"""Host indicator-LED driver + issuer Transport (board led-night-mode).

A computer's own LEDs (NIC port LEDs under /sys/class/leds) are an `indicator` actuator like any other —
so the issuer drives them through a Transport, uniform with the Levoit panel LED. The host runs as user
`visko`; writing sysfs needs root, so the Transport shells out to tools/host-leds.sh via a narrow NOPASSWD
sudoers grant (/etc/sudoers.d/ha-host-leds, installed once). The command's HMAC isn't re-verified here (a
trusted local driver, like Midea/Levoit); the issuer already authorized it.

Trait: indicator {on} -> host-leds.sh on|off, reported {"on": <bool>} (optimistic — the script is fire-and-
report; sysfs has no meaningful ack).
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from . import protocol

log = logging.getLogger("ha.control.hostled")

# absolute path so it matches the sudoers rule regardless of the service's cwd
_SCRIPT = str(Path(__file__).resolve().parents[2] / "tools" / "host-leds.sh")


class HostLedTransport:
    """issuer Transport for host indicator LEDs. `device_ids` is the set of device_ids this drives (each a
    host whose LEDs this box can set — in practice just the local host). `runner` is injectable for tests."""
    def __init__(self, device_ids, script: str = _SCRIPT, runner=None):
        self.device_ids = set(device_ids)
        self.script = script
        self._run = runner or self._sudo

    def _sudo(self, on: bool) -> None:
        subprocess.run(["sudo", "-n", self.script, "on" if on else "off"],
                       capture_output=True, text=True, timeout=15, check=True)

    def send_and_wait(self, *, node, device_id, area, cmd, now=None, timeout=5.0):
        if device_id not in self.device_ids:
            return None                                   # not a host-LED device -> issuer maps to no-ack
        trait, action, args = cmd.get("trait"), cmd.get("action"), cmd.get("args", {})
        if trait != "indicator" or action != "set":
            return protocol.build_ack(cmd_id=cmd["id"], status="rejected",
                                      reason=f"host-led: unsupported {trait}/{action}")
        on = bool(args.get("on"))
        try:
            self._run(on)
        except Exception as e:                            # sudo/script failure -> no-ack (issuer maps to 504)
            log.warning("host-led set failed for %s: %s", device_id, e)
            return None
        return protocol.build_ack(cmd_id=cmd["id"], status="ok",
                                  reported_state={"on": on}, source="commanded")
