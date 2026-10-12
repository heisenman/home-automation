"""server/maintenance/sgp_ambient.py — send each SGP40/41 node its room's real temperature + humidity, signed, so
the node can run the chip's own on-chip humidity compensation beside the uncompensated reading (ADR-0035
amendment, Phase 4b experiment; Study 2 §9). Called from the gas-quality sampler's minute tick.

Wire: retained `{p, s}` on `home/edge/<node>/ambient`, inner `{"op":"ambient","ts","seq","t_c","rh_pct","src"}`
(firmware: ha_mqtt handle_ambient → ha_gas_set_ambient). Signed with the node's LUT secret by the same
`sign_envelope` every server signer uses. NOT on the /cmd topic: the node would persist the cmd anti-replay mark to
flash on every push and race real commands — the ambient topic keeps its own RAM-only freshness/monotonic check.

Source rule: the SAME room's plain T/RH meter, fresh — never another room's, never a gas node's die (the chip
expects AMBIENT conditions at the sensor; a BME680 die runs ~10 °C warm). No meter → nothing sent → the node
stays on defaults (the mech closet today). Kill switch: create `instance/aq-ambient.off`.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOPIC = "home/edge/{node}/ambient"
KILL_SWITCH = REPO / "instance/aq-ambient.off"
SGP4X_TYPES = ("sgp40", "sgp41")
MAX_METER_AGE_S = 600            # the node keeps a value 15 min; don't hand it one that is already old


def node_for_device(registry: dict) -> dict:
    """{device_id: node_id} for edge-node gas lanes, from the registry ({REGKEY: info}). An explicit node_id wins;
    else the '<node>-gas' key convention. Entries without either are skipped (not an edge-node lane)."""
    out: dict[str, str] = {}
    for key, info in registry.items():
        did = (info or {}).get("device_id")
        node = (info or {}).get("node_id")
        if not did:
            continue
        if node:
            out[did] = node
        elif did not in out and str(key).upper().endswith("-GAS"):
            out[did] = str(key)[:-4].lower()
    return out


def _is_meter(d) -> bool:
    dt = (d.get("device_type") or "").lower()
    m = d.get("metrics") or {}
    return ("gas" not in dt and "bme680" not in dt
            and m.get("temperature_c") is not None and m.get("humidity_pct") is not None)


def targets(devices: list, node_of: dict) -> list:
    """[(node, t_c, rh_pct, meter_id)] — one per SGP40/41 lane that has a fresh same-room meter."""
    out = []
    for d in devices:
        if not any(f in (d.get("device_type") or "").lower() for f in SGP4X_TYPES):
            continue
        node = node_of.get(d["device_id"])
        if not node:
            continue
        meters = [m for m in devices if m.get("area") == d.get("area") and _is_meter(m)
                  and (m.get("age_s") is None or m["age_s"] <= MAX_METER_AGE_S)]
        if not meters:
            continue
        m = min(meters, key=lambda x: (0 if "pro" in x["device_id"] else 1, x.get("age_s") or 0.0))
        out.append((node, float(m["metrics"]["temperature_c"]), float(m["metrics"]["humidity_pct"]), m["device_id"]))
    return out


def envelope(secret: str, t_c: float, rh_pct: float, src: str, now: float | None = None) -> dict:
    from server.mesh.coordinator import sign_envelope
    return sign_envelope(secret, {"op": "ambient", "ts": int(now or time.time()), "seq": 0,
                                  "t_c": round(t_c, 2), "rh_pct": round(rh_pct, 1), "src": src})


def push(publish, lut: dict, tgts: list, now: float | None = None) -> list:
    """Sign + publish (retained) each target; returns the nodes sent. A node with no LUT secret is skipped —
    the firmware would reject an unsigned value anyway."""
    sent = []
    for node, t_c, rh, src in tgts:
        secret = (lut.get(node) or {}).get("cmd_secret")
        if not secret:
            continue
        publish(TOPIC.format(node=node), json.dumps(envelope(secret, t_c, rh, src, now)))
        sent.append(node)
    return sent


def run(devices: list, *, broker: str = "127.0.0.1", port: int = 1883,
        registry_path: Path = REPO / "instance/devices.yaml", lut_path: Path = REPO / "instance/node_secrets.enc") -> list:
    """The sampler's hook. Never raises — the experiment must not take the AQ sampler down with it."""
    if KILL_SWITCH.exists():
        return []
    try:
        from server.control.secret_store import available_master, load_lut
        from server.device_registry import load_devices
        master = available_master()
        if not master:
            print("  ambient: no master passphrase — not sending")
            return []
        tgts = targets(devices, node_for_device(load_devices(registry_path)))
        if not tgts:
            return []
        lut = load_lut(lut_path, master)
        import paho.mqtt.client as mqtt
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        c.connect(broker, port, 10)
        c.loop_start()
        infos = []
        sent = push(lambda t, pl: infos.append(c.publish(t, pl, qos=1, retain=True)), lut, tgts)
        for i in infos:
            i.wait_for_publish(timeout=5)
        c.loop_stop()
        c.disconnect()
        print(f"  ambient: sent {', '.join(f'{n} {t:.1f}°C/{h:.0f}%' for n, t, h, _ in tgts if n in sent)}")
        return sent
    except Exception as e:                                    # noqa: BLE001 — log, never break the sampler
        print(f"  ambient: not sent — {type(e).__name__}: {e}")
        return []
