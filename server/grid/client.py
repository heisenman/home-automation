"""ha-2 control-plane client for the grid-shed lane (ADR-0037).

This lane runs on **.210**, which is the only box that can see both sides: the public internet (for the
shed signal) and the air-gapped control plane on 192.168.1.x (ha-2 holds the VIP and drives the
actuators). ha-2 itself has no route to the internet, so the bridge role is structural, not a
convenience.

Talks to the SAME admin control API a human uses from the PWA — `POST /control/{id}/override` and the
display view-model — so there is no privileged side channel and nothing here can do something a person
could not do from the app. The bearer is derived locally from the master passphrase
(`secret_store.api_token`), never stored.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("ha.grid")


@dataclass
class Ha2Client:
    base: str = "http://192.168.1.200:8123"
    token: str = ""
    timeout_s: float = 10.0

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            raw = r.read().decode()
        return json.loads(raw) if raw else {}

    # ── reads ────────────────────────────────────────────────────────────────────
    def display(self, device_id: str) -> dict:
        """The device view-model: control config, the authoritative sensor reading, live override."""
        return self._req("GET", f"/api/v1/display/{device_id}")

    def guardrail(self, device_id: str, max_age_s: float) -> tuple[float | None, str]:
        """The value the shed guardrail is judged on: **the device's own control metric**, read from its
        own AUTHORITATIVE control sensor — the very number its automation runs on.

        Deliberately per-device rather than one global humidity check. A dehumidifier's guardrail is RH;
        an air purifier's is PM2.5, and "do not shut the purifier off when the air is genuinely bad" is
        the guardrail that matters during a Pacific Northwest heat wave, which is also wildfire smoke
        season. A single RH ceiling applied to the purifier is not merely wrong, it is permanently
        unverifiable — the purifier reports no humidity at all, so it would never shed and the feature
        would look like it was working.

        Not the appliance's onboard sensor either: the Midea's hygrometer reads its own dried return air
        (~14 points low while running), so it would happily certify a house that is not actually dry.

        Returns (value, why). value is None whenever we cannot stand behind the number, which the
        decision law treats as "do not shed"."""
        try:
            vm = self.display(device_id)
        except Exception as e:
            return None, f"display fetch failed: {e}"
        metric = ((vm.get("control") or {}).get("metric")) or "value"
        s = vm.get("sensor") or {}
        if not s:
            return None, f"no control-sensor reading ({metric}) — cannot verify the guardrail"
        val = s.get(metric, s.get("value"))
        if val is None:
            return None, f"no {metric} from {s.get('device_id') or 'control sensor'}"
        age = s.get("age_s")
        if age is not None and age > max_age_s:
            return None, (f"control sensor {s.get('device_id')} stale "
                          f"({age:.0f}s > {max_age_s:.0f}s)")
        return float(val), f"{s.get('device_id')} {metric} {float(val):.1f}"

    def live_override_expiry(self, device_id: str) -> float | None:
        try:
            ov = (self.display(device_id) or {}).get("override")
        except Exception as e:
            log.warning("%s: override read failed: %s", device_id, e)
            return None
        return (ov or {}).get("expiry")

    # ── writes ───────────────────────────────────────────────────────────────────
    def shed(self, device_id: str, duration_min: float) -> float | None:
        """Write an override off expiring `duration_min` from now. Returns the server-computed expiry so
        the lane can later recognise its own override (see shed._ours)."""
        r = self._req("POST", f"/control/{device_id}/override",
                      {"action": "off", "duration_min": round(duration_min, 3)})
        return ((r or {}).get("override") or {}).get("expiry")

    def release(self, device_id: str) -> None:
        self._req("POST", f"/control/{device_id}/override", {"action": "clear"})
