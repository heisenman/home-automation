"""Actuator FUNCTION — what a controlled device does to the house (ADR-0034: category lives on the Ability).

`device_type` picks a driver (Midea LAN, the Aprilaire's edge node); it says nothing about what the device is FOR,
and two dehumidifiers carry two unrelated types. `function` is that category, declared per device in control.yaml
(`function: dehumidify`) and required at intake (server/device_registry.validate_new_actuator, the ESPHome intake).

The vocabulary is CLOSED and NARROW on purpose (Hugh, 2026-11-11): a function names what the device changes, not
what it reacts to — a bathroom fan on an RH rule ventilates, it doesn't dehumidify. Each function needs one of the
traits that can actually perform it, so a tag can't land on a device that can't do it.

Consumers: the ERV dew-point law's "track the dehumidifiers" RH floor (server/control/dewpoint_law.py) finds the
house's dehumidifiers by `function == "dehumidify"`.
"""
from __future__ import annotations

# function -> traits of which the device must declare at least one
FUNCTIONS: dict[str, tuple[str, ...]] = {
    "dehumidify": ("switchable", "mode"),           # removes water from the air (Midea, Aprilaire E070)
    "purify_air": ("switchable", "ranged"),         # filters the room's air (Levoit)
    "ventilate": ("mode", "switchable", "ranged"),  # exchanges indoor air with outdoor air (the ERV)
    "condition_temperature": ("setpoint",),         # heats/cools to a setpoint (the thermostat)
    "circulate_air": ("switchable",),               # moves indoor air without changing it (the HVAC blower)
    "switch_power": ("switchable",),                # a plug: switches/meters another appliance's power
    "indicate": ("indicator",),                     # a light or display that signals state (host LED)
    "lock": ("lockable",),                          # locks/unlocks a door
    "shade": ("positionable",),                     # positions a window shade/blind
}


def validate(device_id: str, function, traits: list[str]) -> str | None:
    """None if `function` is a known function the traits can perform, else the error. None function = untagged."""
    if function is None:
        return None
    if function not in FUNCTIONS:
        return f"control device '{device_id}': unknown function '{function}' (known: {sorted(FUNCTIONS)})"
    need = FUNCTIONS[function]
    if not any(t in traits for t in need):
        return (f"control device '{device_id}': function '{function}' needs one of the traits {list(need)} "
                f"(it declares {sorted(traits)})")
    return None
