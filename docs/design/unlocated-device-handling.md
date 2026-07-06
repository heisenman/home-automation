# Unlocated / unknown-location device handling

**What this is:** the backend contract for making sure **no registered device silently disappears** from
the room graph, and any device the backend can't place is **surfaced as an explicit red flag** the UI
flags for the user (Hugh's directive, 2026-07-06). Board task: `unlocated-device-handling` (dev+ops).

This is the `dev`/backend half. The UI half (Ops) renders `rooms.unlocated[]` as amber "Unknown
Location" rows that tap into the room picker (panel v87 `4e29cf2`). **Feedback requested from Ops on the
contract below.**

## The presenting bug (the dehumidifier)

`dehumidifier_living_room` was missing from the devices list and the map. Two compounding causes:

1. **Non-authoritative telemetry.** The Midea dehumidifier self-reports over `transport=midea-lan`, which
   the writer demotes to `authoritative=0` (`writer.py:140`). `build_sensor_list` filters
   `authoritative=1`, so the dehumidifier never entered the sensor list that `build_rooms` iterates — it
   was absent from every room **and** from `unlocated`. (The Levoit purifier shows because its telemetry
   stays authoritative.)
2. **Controller published no `area` in the payload (root data).** `ha-controller._publish_state`
   resolves `area` correctly from the registry (`living_room`) and puts it in the *topic*
   (`home/{area}/…`), but the JSON **payload omitted the `area` field**. The writer derives area from
   `payload.get("area", "unknown")` (`writer.py:152`), **not** the topic — so every Midea self-report
   defaulted to `device_last_seen.area = 'unknown'`. `control.yaml` correctly says `living_room`, so the
   dehumidifier is a **false unknown**. (Confirmed the hard way: restarting the controller did NOT fix it,
   which ruled out a stale-cache hypothesis and pointed at the payload.) Fix: add `"area": area` to the
   `_publish_state` payload — mirrors the scanner/bridge payloads, which have always included it.

Analysis of the full fleet: all 3 control actuators + 15 registry devices have **canonical** areas —
**nothing is genuinely unlocated today.** `unlocated[]` is the safety net, expected empty.

## The backend contract (`/api/v1/rooms`)

1. **Every controllable device appears, located, even with no authoritative readings.** New
   `viewmodel.build_actuator_list(hot_conn, control_registry, present_ids, meta, now)` emits
   sensor-shaped entries for controllable devices absent from `build_sensor_list`. **Area/type come from
   the CONTROL REGISTRY (`control.yaml`)** — the canonical source — *not* `device_last_seen`, which can be
   `unknown` if a publisher omits `area` (the controller bug this change also fixes). Latest metric values are pulled at **either** trust level
   so the tile still shows current RH/target. The `/api/v1/rooms` handler appends these to the sensor
   list before `build_rooms` (`main.py::rooms_list`).
2. **`rooms.unlocated[]` is the authoritative red-flag list.** Any device (sensor or actuator) whose room
   isn't a canonical area lands here rather than being dropped. Each entry now carries an explicit signal:
   - `location_unknown: true`
   - `reason`: `"device has no resolved location"` (area is empty/`unknown`) or
     `"area '<x>' is not a canonical room in areas.yaml"`.
   - plus the existing `device_id`, `name`, `area`, `role`, `device_type`, `metrics`, `placement`.

   The UI should treat a non-empty `unlocated[]` as a red-flag section (v87 already does, driven by
   `device_id`+`name`; `location_unknown`/`reason` are additive for badge/tooltip). `schema_version` is
   unchanged (1) — the additions are backward-compatible.

## Interaction with Ops's `c84672f` (/displays)

`c84672f` made `build_display` fall back `room = meta.room OR device_last_seen area`. That fixes actuators
that HAVE a good `device_last_seen.area`. Before the controller payload fix, the dehumidifier's
`device_last_seen.area` was `'unknown'`, so `/displays` would still have shown it unlocated. The
**controller `area`-in-payload fix (this change) is the root fix**: once the controller stamps
`living_room` into `device_last_seen`, both `/rooms` and `/displays` agree. Optional belt-and-suspenders
(Ops's call, your file): have `build_display` source an actuator's area from the control registry (as
`build_actuator_list` does) if `device_last_seen.area` is ever missing/`unknown` again.

## Answers to Ops's NEED-FROM-DEV

1. **Is `rooms.unlocated[]` the authoritative flag for all null/unknown devices? Does `/rooms` surface a
   registered device with no recent readings?** Yes to both — `build_actuator_list` enumerates every
   controllable device from the registry (located or, if non-canonical, into `unlocated`). `unlocated[]`
   + `location_unknown` is the authoritative signal.
2. **Deploy = restart `ha-api` + `ha-api-tls` on 210.** Dev action; done as part of this change.
3. **Which specific null/unknown devices?** Only `dehumidifier_living_room`, and it's a *false* unknown
   (canonical `living_room` in `control.yaml`; `device_last_seen.area=unknown` because the controller
   payload omitted `area` — fixed here).
   After this change it places in `living_room` via the registry; no device is genuinely unlocated.

Related: [ingest-registry-map.md](ingest-registry-map.md) (same stale-cache class, ingest side),
`server/api/viewmodel.py` (`build_actuator_list`, `build_rooms`), `server/control/controller.py`
(`_publish_state`).
