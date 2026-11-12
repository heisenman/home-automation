"""Actuator models for the unified law (docs/design/unified-control-law.md §3).

An actuator joins the law by its `function` tag (server/control/functions.py) and gets a model by function +
device_type. A model gives, per option (off/on, or a level), for the house's current conditions:
  water_gph  — g/h of water it adds to the house (negative = removes)
  heat_w     — W of heat it adds to the house (negative = removes)
  elec_w     — W of electricity it draws
  vent_level — its ventilation level (the AQ term counts levels)
plus `confidence` (good / rough / none per quantity) and where the numbers came from.

The numbers are the study's (constants, with provenance). Stage L3's fits land in control.db `house_setting`
`law:params` — the same keys, overriding these — so better numbers arrive without code changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field

RHO_CP = 1.2 * 1005 / 3600          # W per (m³/h · K), air near 21 °C
CFM_M3H = 1.699
LATENT_WH_PER_G = 2450 / 3600       # Wh released per g of water condensed

PARAMS = {
    "midea": {"elec_w": 480, "l_per_kwh": 1.04, "source": "bucket 11-07→09 (9.9 L, 9.57 kWh) + 11-07 P2",
              "confidence": {"water": "good", "heat": "good"}},
    "aprilaire": {"elec_w": 580, "l_per_kwh": 0.75, "source": "method B across the air handler, 17 runs at "
                  "dew point 7.5–9.5 °C (evening dips unexplained)", "confidence": {"water": "rough", "heat": "rough"}},
    "blower": {"elec_w": 150, "source": "estimate for tap 1 (not metered)", "confidence": {"elec": "rough"}},
    "erv": {"cfm": [0, 66, 92, 125], "fan_w": [8, 31, 60, 130], "latent_pass": 0.3, "sensible_eff": None,
            "source": "cfm/power from the ERV's own telemetry; latent pass-through (1 − ε_L) from the HVAC-pair zero "
                      "model's ERV term (0.035–0.055 per 100 cfm ≈ 0.15·(1 − ε_L))",
            "confidence": {"water": "rough", "heat": "none", "aq": "rough"}},
    "furnace": {"input_kw": 17.6, "efficiency": 0.96, "source": "Carrier 59TP5A060 (60 kBtu/h input), condensing "
                "two-pipe (efficiency to confirm from the manual)", "confidence": {"heat": "none"}},
    "house": {"air_m3": 520, "buffer_x": 26, "load_gph": 0.0, "ua_w_per_k": None, "c_kwh_per_k": None,
              "source": "study Phase 1 (house buffers ~26× its air volume); heat-loss fit pending (L3)",
              "confidence": {"water": "rough", "heat": "none"}},
}


def params(over: dict | None) -> dict:
    out = {k: dict(v) for k, v in PARAMS.items()}
    for k, v in (over or {}).items():
        if k in out and isinstance(v, dict):
            out[k].update(v)
    return out


@dataclass(frozen=True)
class Option:
    label: str
    level: int                      # 0 = off; ventilators/purifiers: their level; switches: 1 = on
    water_gph: float = 0.0
    heat_w: float = 0.0
    elec_w: float = 0.0
    vent_level: int = 0


@dataclass(frozen=True)
class Model:
    device_id: str
    function: str
    kind: str                       # midea | aprilaire | erv | purifier | thermostat | blower
    options: tuple = ()
    confidence: dict = field(default_factory=dict)
    source: str = ""
    needs: tuple = ()               # other devices this one needs running (the Aprilaire needs the blower)
    area_limited: bool = False      # its effect reaches one room, not the house (the Midea)


def _dehum(device_id, kind, p, blower):
    elec = float(p["elec_w"])
    water = -float(p["l_per_kwh"]) * elec            # g/h (L/kWh × kW × 1000)
    heat = elec + (-water) * LATENT_WH_PER_G          # compressor + fan heat, plus the latent heat released
    on = Option("on", 1, water, heat, elec + (blower or 0.0))
    return Model(device_id, "dehumidify", kind, (Option("off", 0), on), dict(p.get("confidence", {})),
                 p.get("source", ""), needs=("hvac_blower",) if kind == "aprilaire" else (),
                 area_limited=(kind == "midea"))


def _erv(device_id, p, cond):
    opts = []
    w_in, w_out, t_in, t_out = (cond.get(k) for k in ("w_in", "w_out", "t_in", "t_out"))
    for lvl, (cfm, fw) in enumerate(zip(p["cfm"], p["fan_w"])):
        q = cfm * CFM_M3H
        water = q * float(p["latent_pass"]) * (w_out - w_in) if w_in is not None and w_out is not None else 0.0
        se = p.get("sensible_eff")
        heat = (q * RHO_CP * (1 - float(se)) * (t_out - t_in)
                if se is not None and t_in is not None and t_out is not None else 0.0)
        opts.append(Option(["off", "low", "med", "high"][lvl] if lvl < 4 else str(lvl), lvl, water, heat, fw, lvl))
    return Model(device_id, "ventilate", "erv", tuple(opts), dict(p.get("confidence", {})), p.get("source", ""))


def build(registry: dict, prm: dict, cond: dict) -> list[Model]:
    """Models for every tagged actuator the law knows how to reason about. `registry` = {device_id: DeviceCtl}
    (needs .function and .device_type); `cond` = {w_in, w_out, t_in, t_out} for the condition-dependent ones.
    Unknown function/type combinations are left out (they stay on their rule)."""
    out = []
    has_blower = any(getattr(c, "function", None) == "circulate_air" for c in registry.values())
    for did, ctl in sorted(registry.items()):
        fn, dt = getattr(ctl, "function", None), getattr(ctl, "device_type", None)
        if fn == "dehumidify" and dt == "dehumidifier":
            out.append(_dehum(did, "midea", prm["midea"], None))
        elif fn == "dehumidify" and dt == "dehum":
            out.append(_dehum(did, "aprilaire", prm["aprilaire"],
                              prm["blower"]["elec_w"] if has_blower else None))
        elif fn == "ventilate" and dt == "erv":
            out.append(_erv(did, prm["erv"], cond))
        elif fn == "purify_air":
            out.append(Model(did, fn, "purifier", tuple(Option(str(i), i) for i in range(1, 5)),
                             {"aq": "none"}, "no model: the AQ index is VOC-based, filters barely move it"))
        elif fn == "condition_temperature":
            out.append(Model(did, fn, "thermostat", (), dict(prm["furnace"].get("confidence", {})),
                             prm["furnace"].get("source", "")))
    return out


def inventory(models: list[Model]) -> list[dict]:
    """The PWA's model table: each actuator, what it is, how sure we are, and where that came from."""
    return [{"device_id": m.device_id, "function": m.function, "kind": m.kind, "confidence": m.confidence,
             "source": m.source, "area_limited": m.area_limited, "needs": list(m.needs)} for m in models]
