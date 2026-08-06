"""Control API — human/client → server command requests (plan §13.2).

A thin HTTP shim over the PEP (issuer): clients may only *request*; the server authorises (policy),
signs, sends, and reconciles. This router is **NOT mounted** into the live read-only dashboard API yet
— exposing control on the currently-unauthenticated API would re-open the gap we are closing. It mounts
only AFTER broker auth + API auth are in place (see docs/FOLLOWUPS.md / broker-auth-cutover.md).

The Result→HTTP mapping is a pure function so it is unit-testable without a web server.
"""
from __future__ import annotations

import re
from typing import Any

from server.control.issuer import CommandIssuer, Result

# Result.status -> HTTP status code
_STATUS_HTTP = {
    "ok": 200,
    "mismatch": 409,        # device acked but reported state != intended
    "rejected": 403,        # policy/contract/signature denial
    "unknown-device": 404,
    "no-ack": 504,          # device did not respond
}


def result_to_http(r: Result) -> tuple[int, dict[str, Any]]:
    code = _STATUS_HTTP.get(r.status, 500)
    return code, {
        "status": r.status,
        "reason": r.reason,
        "intended": r.intended,
        "reported": r.reported,
        "cmd_id": r.cmd_id,
    }


def handle_command(issuer: CommandIssuer, device_id: str, body: dict[str, Any],
                   confirm_verifier=None) -> tuple[int, dict]:
    """Validate a command-request body and run it through the PEP. Pure (no HTTP framework).

    The second factor for sensitive actions is SOFTWARE (per Hugh 2026-06-21: not all endpoints have
    buttons) — a `confirm_pin` in the body checked by `confirm_verifier(device_id, pin) -> bool`.
    With no verifier configured, sensitive actions cannot be confirmed (fail-safe: policy denies them)."""
    trait = body.get("trait")
    action = body.get("action")
    if not isinstance(trait, str) or not isinstance(action, str):
        return 400, {"status": "bad-request", "reason": "trait and action are required strings"}
    args = body.get("args") or {}
    if not isinstance(args, dict):
        return 400, {"status": "bad-request", "reason": "args must be an object"}
    confirmed = False
    pin = body.get("confirm_pin")
    if pin is not None and confirm_verifier is not None:
        confirmed = bool(confirm_verifier(device_id, str(pin)))
    r = issuer.issue(device_id=device_id, trait=trait, action=action, args=args, confirmed=confirmed)
    return result_to_http(r)


# ── Manual override (user-initiated timeout) ─────────────────────────────────────
# The override is the human escape hatch over the automation loop (ADR-0011 precedence layer 2):
# "off" parks the device, "boost_on" forces it, both with a TTL so a forgotten override self-clears;
# "clear" hands control straight back to the rule. The API only WRITES control.db's override row — the
# controller reads it every tick (store.get_override) and the resolver enforces it (incl. min-off/safety),
# so there is no separate command path and a stale API can't strand the device ON.
_OVERRIDE_ACTIONS = ("off", "boost_on", "clear")
# 7d cap — an override is a TIMEOUT, never a permanent mode: a forgotten pause must still self-clear
# rather than masquerade as a dead actuator forever. Raised from 24h so a pause can be expressed in days
# (the UI offers minutes/hours/days); to park a device indefinitely, disable its automation instead.
MAX_OVERRIDE_MIN = 7 * 24 * 60


# Duration units the override UI offers (server-authored — see viewmodel.build_controls). Kept here next
# to the cap they are validated against so the two can never drift.
OVERRIDE_UNITS = (
    {"key": "min",  "label": "minutes", "mult": 1},
    {"key": "hour", "label": "hours",   "mult": 60},
    {"key": "day",  "label": "days",    "mult": 1440},
)


def handle_override(conn, device_id: str, body: dict[str, Any], now: float,
                    valid_devices=None) -> tuple[int, dict]:
    """Set or clear a manual override on control.db. Pure (no HTTP framework). `valid_devices`, if
    given, restricts to known controllable device_ids (else 404)."""
    from server.control import control_store as store
    if valid_devices is not None and device_id not in valid_devices:
        return 404, {"status": "unknown-device", "reason": f"no controllable device {device_id!r}"}
    action = body.get("action")
    if action not in _OVERRIDE_ACTIONS:
        return 400, {"status": "bad-request", "reason": f"action must be one of {_OVERRIDE_ACTIONS}"}
    if action == "clear":
        store.clear_override(conn, device_id)
        return 200, {"status": "ok", "device_id": device_id, "override": None}
    dur = body.get("duration_min")
    if isinstance(dur, bool) or not isinstance(dur, (int, float)) or dur <= 0:
        return 400, {"status": "bad-request", "reason": "duration_min must be a positive number"}
    if dur > MAX_OVERRIDE_MIN:
        return 400, {"status": "bad-request",
                     "reason": f"duration_min exceeds {MAX_OVERRIDE_MIN} "
                               f"({MAX_OVERRIDE_MIN // 1440}d) cap"}
    expiry = now + float(dur) * 60.0
    store.set_override(conn, device_id, action, expiry)
    return 200, {"status": "ok", "device_id": device_id,
                 "override": {"action": action, "expiry": expiry, "duration_min": dur}}


def read_control_state(conn, device_id: str, now: float) -> dict:
    """Compose a device's live control snapshot (policy + active override + active house scene + recent
    decisions) from control.db. Pure; shared by the admin GET and the display view-model (BFF)."""
    from server.control import control_store as store
    from server.control.automation import apply_scene
    ov = store.get_override(conn, device_id, now)
    override = None
    if ov:
        action, expiry = ov
        override = {"action": action, "expiry": expiry,
                    "expires_in_min": None if expiry is None else max(0.0, (expiry - now) / 60.0)}
    log_rows = store.recent_log(conn, device_id, limit=10)
    pol = store.get_policy(conn, device_id)
    scene = store.get_scene(conn)
    # what the active scene does to THIS device right now (so the UI can show "Away: relaxed / parks it")
    scene_active = None
    if pol is not None:
        eff, scene_off = apply_scene(pol, scene)
        patch = (pol.get("scenes") or {}).get(scene) or {}
        scene_active = {"scene": scene, "off": scene_off,
                        "control": eff.get("control") if eff is not pol else None,
                        "patch": patch or None}
    return {
        "device_id": device_id,
        "policy": pol,
        "override": override,
        "scene": scene,
        "scene_active": scene_active,
        "last_decision": log_rows[0] if log_rows else None,
        "recent_log": log_rows,
    }


# ── House scene (Home/Away/Sleep) ────────────────────────────────────────────────
from server.control.automation import HOUSE_SCENES                       # noqa: E402

_SCENE_NUM_KEYS = ("on_above", "off_below", "min_on_min", "min_off_min")
# `brightness` (0..100) is the per-scene setpoint for display actuators (wall panels): the scene-follower
# drives the panel backlight to this level (0 = screen off). Ignored by hysteresis (sensor) devices.
_SCENE_KEYS = {"off", "brightness", *_SCENE_NUM_KEYS}


def handle_set_scene(conn, body: dict[str, Any]) -> tuple[int, dict]:
    """Set the whole-house scene (one global row in control.db). The controller reads it every tick and
    folds each device's matching scene patch into the effective policy. Pure (no HTTP framework)."""
    from server.control import control_store as store
    scene = (body or {}).get("scene")
    if scene not in HOUSE_SCENES:
        return 400, {"status": "bad-request", "reason": f"scene must be one of {list(HOUSE_SCENES)}"}
    store.set_scene(conn, scene)
    return 200, {"status": "ok", **store.get_scene_full(conn), "scenes": list(HOUSE_SCENES)}


NIGHT_MODE_DEFAULT = {"enabled": False, "window": "22:00-07:00"}


def handle_set_night_mode(conn, body: dict[str, Any]) -> tuple[int, dict]:
    """Set the global LED night-mode config {enabled, window:'HH:MM-HH:MM'}. The controller turns every
    indicator-capable device's LED off on entering the window, on when leaving. Pure (no HTTP framework)."""
    from server.control import control_store as store
    b = body or {}
    enabled, window = b.get("enabled", False), b.get("window", "22:00-07:00")
    if not isinstance(enabled, bool):
        return 400, {"status": "bad-request", "reason": "enabled must be a boolean"}
    if not isinstance(window, str) or not _WINDOW_RE.match(window):
        return 400, {"status": "bad-request", "reason": "window must be 'HH:MM-HH:MM'"}
    cfg = {"enabled": enabled, "window": window}
    store.set_setting(conn, "night_mode", cfg)
    return 200, {"status": "ok", "night_mode": cfg}


def _validate_scenes(sc, bad):
    """Validate a policy `scenes` map: {scene_name: {off?: bool, on_above?/off_below?/min_*?: num}}.
    Returns None if OK, else the (code, body) tuple from `bad`."""
    if not isinstance(sc, dict):
        return bad("scenes must be an object")
    for name, prof in sc.items():
        if name not in HOUSE_SCENES:
            return bad(f"scene name must be one of {list(HOUSE_SCENES)}")
        if not isinstance(prof, dict):
            return bad(f"scene {name!r} must be an object")
        extra = set(prof) - _SCENE_KEYS
        if extra:
            return bad(f"scene {name!r} has unknown keys {sorted(extra)}")
        if "off" in prof and not isinstance(prof["off"], bool):
            return bad(f"scene {name!r}.off must be a boolean")
        if "brightness" in prof and not (_is_num(prof["brightness"]) and 0 <= float(prof["brightness"]) <= 100):
            return bad(f"scene {name!r}.brightness must be a number in 0..100")
        for k in _SCENE_NUM_KEYS:
            if k in prof and not _is_num(prof[k]):
                return bad(f"scene {name!r}.{k} must be a number")
        if ("on_above" in prof and "off_below" in prof
                and float(prof["on_above"]) <= float(prof["off_below"])):
            return bad(f"scene {name!r}: on_above must be strictly greater than off_below")
    return None


# ── Policy editing (app-mutable settings) ────────────────────────────────────────
_ALLOWED_STRATEGIES = ("hysteresis", "setpoint", "threshold_ranged")
# Sensor metrics a control loop may drive on. Expandable — add a metric here (+ a UI entry in
# app.js AIR_QUALITY_METRICS + a writer._UNITS entry) to offer it as a selectable control source.
#
# DIRECTION IS NOT UNIFORM. pm25_ugm3/aqi rise as the air gets WORSE; `air_quality` is the unified
# ADR-0035 cleanliness score, which rises as the air gets BETTER (100 = clean). Bands are still
# validated as ascending `max` cutoffs either way — what flips is the LEVEL ladder attached to them
# (see app.js AIR_QUALITY_METRICS[].levels). A ladder built for PM2.5 and re-pointed at air_quality
# without flipping runs the fan flat-out in clean air, so the two always travel together.
#
# air_quality is a DERIVED metric (computed by the viewmodel fusion, persisted every 60s by
# ha-gas-quality-sampler). It never appears on MQTT, so the controller resolves it out of hot.db —
# see Controller._pick_source.
_ALLOWED_CONTROL_METRICS = ("humidity_pct", "pm25_ugm3", "aqi", "air_quality")
_WINDOW_RE = re.compile(r"^\d{1,2}:\d{2}-\d{1,2}:\d{2}$")


def _is_num(v) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float))


def handle_policy_update(conn, device_id: str, body: dict[str, Any],
                         valid_devices=None) -> tuple[int, dict]:
    """MERGE-patch a device's automation policy (the app-mutable thresholds/enable/quiet-window). Pure.
    A merge (not replace) so a partial edit never silently drops fields the UI didn't send. The controller
    re-reads the policy every tick, so edits take effect on the next cycle."""
    from server.control import control_store as store
    if valid_devices is not None and device_id not in valid_devices:
        return 404, {"status": "unknown-device", "reason": f"no controllable device {device_id!r}"}
    pol = store.get_policy(conn, device_id)
    if pol is None:
        return 404, {"status": "unknown-device", "reason": f"no policy for {device_id!r}"}
    patch = body or {}

    def bad(reason):
        return 400, {"status": "bad-request", "reason": reason}

    if "enabled" in patch:
        if not isinstance(patch["enabled"], bool):
            return bad("enabled must be a boolean")
        pol["enabled"] = patch["enabled"]
    if "source_sensor" in patch:
        if not isinstance(patch["source_sensor"], str) or not patch["source_sensor"]:
            return bad("source_sensor must be a non-empty string")
        pol["source_sensor"] = patch["source_sensor"]
    if "fallback_sensors" in patch:
        fb = patch["fallback_sensors"]
        if not isinstance(fb, list) or any(not isinstance(x, str) or not x for x in fb):
            return bad("fallback_sensors must be a list of non-empty strings")
        pol["fallback_sensors"] = fb
    if "sensor_stale_min" in patch:
        if not _is_num(patch["sensor_stale_min"]) or patch["sensor_stale_min"] <= 0:
            return bad("sensor_stale_min must be a positive number")
        pol["sensor_stale_min"] = patch["sensor_stale_min"]
    if "control" in patch:
        cp = patch["control"]
        if not isinstance(cp, dict):
            return bad("control must be an object")
        c = dict(pol.get("control", {}))
        for k in ("strategy", "metric", "on_above", "off_below", "min_on_min", "min_off_min"):
            if k in cp:
                c[k] = cp[k]
        if c.get("strategy") not in _ALLOWED_STRATEGIES:
            return bad(f"strategy must be one of {_ALLOWED_STRATEGIES}")
        if "metric" in c and c["metric"] not in _ALLOWED_CONTROL_METRICS:
            return bad(f"control.metric must be one of {_ALLOWED_CONTROL_METRICS}")
        for k in ("on_above", "off_below", "min_on_min", "min_off_min"):
            if k in c and not _is_num(c[k]):
                return bad(f"control.{k} must be a number")
        if c.get("strategy") == "hysteresis" and float(c.get("on_above", 0)) <= float(c.get("off_below", 0)):
            return bad("on_above must be strictly greater than off_below (deadband)")
        # threshold_ranged: sensor band -> fan speed. Validate the bands (ascending max cutoffs; the final
        # catch-all band has max=null). Editing these is how the purifier's PM2.5->speed map is tuned.
        if "bands" in cp:
            bands = cp["bands"]
            if not isinstance(bands, list) or not bands:
                return bad("control.bands must be a non-empty list")
            prev = None
            for b in bands:
                if not isinstance(b, dict) or not _is_num(b.get("level")):
                    return bad("each band needs a numeric level")
                mx = b.get("max")
                if mx is not None:
                    if not _is_num(mx):
                        return bad("band max must be a number or null")
                    if prev is not None and float(mx) <= float(prev):
                        return bad("band max thresholds must strictly increase")
                    prev = mx
            c["bands"] = bands
        if c.get("strategy") == "threshold_ranged" and not c.get("bands"):
            return bad("threshold_ranged requires control.bands")
        pol["control"] = c
    if "schedule" in patch:
        sched = patch["schedule"]
        if not isinstance(sched, list):
            return bad("schedule must be a list")
        for e in sched:
            if not isinstance(e, dict) or not _WINDOW_RE.match(str(e.get("when", ""))):
                return bad("each schedule entry needs when='HH:MM-HH:MM'")
            if e.get("policy") not in ("off", "auto"):
                return bad("each schedule entry needs policy='off'|'auto'")
        pol["schedule"] = sched
    if "scenes" in patch:
        err = _validate_scenes(patch["scenes"], bad)
        if err is not None:
            return err
        pol["scenes"] = patch["scenes"]

    store.set_policy(conn, device_id, pol)
    return 200, {"status": "ok", "device_id": device_id, "policy": pol}


# ── device meta (R8: user-set friendly name / room / hidden) ─────────────────────
def handle_device_meta(conn, device_id: str, body: dict[str, Any]) -> tuple[int, dict]:
    """Set a device's display overlay (name/room/hidden). Pure. Empty-string name/room clears that label
    (UI falls back to the registry); fields omitted are left unchanged. Works for ANY device_id."""
    from server.control import control_store as store
    patch = body or {}
    name, room, hidden = patch.get("name"), patch.get("room"), patch.get("hidden")
    retired = patch.get("retired")
    if name is not None and not isinstance(name, str):
        return 400, {"status": "bad-request", "reason": "name must be a string"}
    if room is not None and not isinstance(room, str):
        return 400, {"status": "bad-request", "reason": "room must be a string"}
    if hidden is not None and not isinstance(hidden, bool):
        return 400, {"status": "bad-request", "reason": "hidden must be a boolean"}
    if retired is not None and not isinstance(retired, bool):
        return 400, {"status": "bad-request", "reason": "retired must be a boolean"}
    if name is None and room is None and hidden is None and retired is None:
        return 400, {"status": "bad-request", "reason": "nothing to set (name/room/hidden/retired)"}
    store.set_device_meta(conn, device_id, name=name, room=room, hidden=hidden, retired=retired)
    return 200, {"status": "ok", "device_id": device_id, "meta": store.get_device_meta(conn, device_id)}


def handle_device_calibration(conn, device_id: str, body: dict[str, Any]) -> tuple[int, dict]:
    """Set a per-metric DISPLAY offset (added to shown/graphed values; control is unaffected). Pure.
    Body: {metric, offset}. offset=0 clears it."""
    from server.control import control_store as store
    metric, offset = (body or {}).get("metric"), (body or {}).get("offset")
    if not isinstance(metric, str) or not metric:
        return 400, {"status": "bad-request", "reason": "metric must be a non-empty string"}
    if isinstance(offset, bool) or not isinstance(offset, (int, float)):
        return 400, {"status": "bad-request", "reason": "offset must be a number"}
    store.set_calibration(conn, device_id, metric, float(offset))
    return 200, {"status": "ok", "device_id": device_id, "calibration": store.all_calibration(conn).get(device_id, {})}


import re as _re

_DEVICE_ID_RE = _re.compile(r"^[a-z0-9_]+$")


def _maintain(op: str, device_id: str, body: dict[str, Any]) -> tuple[int, dict]:
    """Shared handler for the Entity-plane maintenance ops (rename / relocate). Both go through the SAME
    footgun-free orchestration (``apply_rename_worksheet``: mutate → restart ingest fleet → idempotent sweep
    → verify, reconcile paused): a ``dry_run`` returns a synchronous, side-effect-free preview; a real run is
    launched as a DETACHED job (it restarts ha-api itself) and the caller polls ``/api/v1/admin/jobs/<id>``.
    Backups are written by the underlying tools before any mutation."""
    from server.maintenance import admin_job
    from server.maintenance import apply_rename_worksheet as A
    b = body or {}
    dry_run = b.get("dry_run", False)
    if not isinstance(dry_run, bool):
        return 400, {"status": "bad-request", "reason": "dry_run must be a boolean"}
    # Guard the SOURCE id at the boundary: it flows into the peer's remote SHELL command (device_id is
    # embedded in a double-quoted sqlite arg over the full-shell :22 SSH), and validate_plan only checks the
    # target area — so a crafted id (e.g. containing `"`) could inject on the peer. Real ids are [a-z0-9_].
    if not _DEVICE_ID_RE.match(device_id or ""):
        return 400, {"status": "bad-request", "reason": "device_id must match [a-z0-9_]+"}
    if op == "relocate":
        new_area, mode = b.get("new_area"), b.get("mode")
        if not isinstance(new_area, str) or not new_area.strip():
            return 400, {"status": "bad-request", "reason": "new_area must be a non-empty string"}
        # mode is explicit — the two are NOT interchangeable: restamp = history-safe (past follows the
        # device, for a mislabel); forward = forward-only (leave readings history, a genuine physical move).
        if mode not in ("restamp", "forward"):
            return 400, {"status": "bad-request", "reason": "mode must be 'restamp' or 'forward'"}
        new_area = new_area.strip()
        plan = A.single_device_plan(device_id, new_area=new_area, restamp=(mode == "restamp"))
        spec = {"op": "relocate", "device_id": device_id, "new_area": new_area,
                "restamp": mode == "restamp"}
    elif op == "rename":
        new_id = b.get("new_id")
        if not isinstance(new_id, str) or not new_id.strip():
            return 400, {"status": "bad-request", "reason": "new_id must be a non-empty string"}
        new_id = new_id.strip()
        if not _DEVICE_ID_RE.match(new_id):
            return 400, {"status": "bad-request", "reason": "new_id must match [a-z0-9_]+"}
        plan = A.single_device_plan(device_id, new_id=new_id)
        spec = {"op": "rename", "device_id": device_id, "new_id": new_id}
    else:
        return 400, {"status": "bad-request", "reason": f"unknown op {op!r}"}

    if not plan:
        return 400, {"status": "noop", "device_id": device_id,
                     "reason": "nothing to change (target equals current)"}
    errs = A.validate_plan(plan)        # canonical target area + no id collision — fail fast before launch
    if errs:
        return 400, {"status": "invalid", "device_id": device_id, "problems": errs}
    if dry_run:
        # side-effect-free, local-only preview (no peer SSH) — what WOULD change
        report = A.run_plan(plan, dry_run=True, do_peer=False)
        return 200, {"status": "preview", "op": op, "device_id": device_id, "report": report}
    try:
        job_id = admin_job.launch(spec)
    except Exception as e:
        return 500, {"status": "error", "device_id": device_id, "reason": str(e)}
    return 202, {"status": "launched", "op": op, "device_id": device_id, "job_id": job_id,
                 "poll": f"/api/v1/devices/jobs/{job_id}",
                 "note": "runs detached (restarts the ingest fleet); poll for the report"}


def handle_device_relocate(device_id: str, body: dict[str, Any]) -> tuple[int, dict]:
    """Relocate a device to ``new_area`` (Entity-plane area move). Body ``{new_area, mode, dry_run?}``.
    See ``_maintain`` — orchestrated + detached so it can't leave the mapper re-stamping the old area."""
    return _maintain("relocate", device_id, body)


def handle_device_rename(device_id: str, body: dict[str, Any]) -> tuple[int, dict]:
    """Rename a device's ``device_id`` everywhere (Entity identity). Body ``{new_id, dry_run?}``.
    See ``_maintain``; the underlying rename also migrates the .245 peer + pauses reconcile so the old id
    can't be resurrected."""
    return _maintain("rename", device_id, body)


# ── history gap check + recovery (reuses the daily gap-watcher's scan + backfill routing) ─────────
_GAP_MOD = None


def _gap_watcher():
    """Lazy-import tools/gap_watcher.py (not a package) once. It owns the gap scan + per-device-type
    backfill routing/dispatch used by the nightly ha-gap-watcher timer — we reuse it verbatim so the
    on-demand path can never drift from the scheduled one."""
    global _GAP_MOD
    if _GAP_MOD is None:
        import importlib.util
        from pathlib import Path
        gw = Path(__file__).resolve().parents[2] / "tools" / "gap_watcher.py"
        spec = importlib.util.spec_from_file_location("ha_gap_watcher", gw)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _GAP_MOD = mod
    return _GAP_MOD


def _backfill_routes():
    """Load ``instance/backfill-routes.yaml`` (device_id -> {via,node,profile}) so the on-demand recover +
    checker route to the same node the nightly gap-watcher does. Without this the API falls back to
    device_type inference (retired default node). Re-read each call (mtime is cheap; the file is tiny) so an
    edit takes effect without an API restart. Returns {} if absent/unreadable."""
    import yaml
    from pathlib import Path
    p = Path(__file__).resolve().parents[2] / "instance" / "backfill-routes.yaml"
    try:
        return yaml.safe_load(p.read_text()) or {} if p.exists() else {}
    except Exception:
        return {}


def _load_registry_device(devices_path, device_id: str):
    """Return ``(mac, info)`` for ``device_id`` from the sensor registry, or ``(None, None)``.
    ``info`` carries device_type/area/backfill — everything the routing + dispatch need."""
    import yaml
    reg = (yaml.safe_load(open(devices_path).read()) or {}).get("devices", {}) if devices_path else {}
    for mac, info in reg.items():
        if (info or {}).get("device_id") == device_id:
            return mac, info
    return None, None


def _device_for_node(devices_path, node_id: str) -> str | None:
    """The device_id of the (gas) device owned by ``node_id`` in the sensor registry, or None. Used by
    ADR-0036 edge-node intake to resolve e.g. node standby_c6 -> device gas_standby."""
    import yaml
    reg = (yaml.safe_load(open(devices_path).read()) or {}).get("devices", {}) if devices_path else {}
    for info in reg.values():
        if (info or {}).get("node_id") == node_id:
            return (info or {}).get("device_id")
    return None


# The metrics each gas family publishes, mirroring firmware/components/ha_gas (the three drivers linked
# into every image). Hand-written per record in devices.yaml until now; centralised here so an
# auto-registered node gets exactly what an install-time-registered one gets.
_GAS_CAPABILITIES = {
    "sgp40_gas":  ["voc_index", "voc_raw"],
    "sgp41_gas":  ["voc_index", "voc_raw", "nox_index", "nox_raw"],
    "sgp30_gas":  ["eco2", "tvoc"],
    "bme680_gas": ["temperature_c", "humidity_pct", "pressure_hpa", "gas_ohm"],
}

# Where a freshly auto-registered edge ability is parked until intake relocates it into a real room.
# Matches the area standby_c6-gas has carried since 2026-07-08.
DORMANT_AREA = "staging"


def _gas_device_id(taken, area: str, gas: str) -> str:
    """A device_id for a gas ability in ``area`` that is FREE BY CONSTRUCTION, given the ``taken`` set.

    `gas_<area>` is the preferred name and the one an operator sees in the common case. It is not unique
    though: it silently assumes ONE gas sensor per room, and redundant/complementary sensing in a single
    room is a design goal, not an anomaly (an SGP41 next to a BME680 measures VOC+NOx the BME680 cannot).
    So collision is an EXPECTED input, and this qualifies by sensor family — `gas_mech_closet_sgp41` —
    rather than failing and handing an operator a decision the system can make for itself.

    Nothing parses the area back out of a device_id (a device that later relocates keeps its id, so the
    prefix is already only a mnemonic), which is what makes qualifying it safe.

    Deterministic and total: same registry + same inputs -> same answer, and the numeric tail terminates
    for any number of same-family sensors in one room.
    """
    base = f"gas_{area}"
    if base not in taken:
        return base
    qualified = f"{base}_{gas.removesuffix('_gas')}"     # gas_mech_closet_sgp41
    if qualified not in taken:
        return qualified
    n = 2                                                # ...and a third SGP41 in that room: _2, _3, ...
    while f"{qualified}_{n}" in taken:
        n += 1
    return f"{qualified}_{n}"


def _register_edge_node_device(devices_path, node_id: str, abilities, area: str, *, dry_run=False):
    """Synthesise the sensor-registry record for a NEW edge node's gas ability (ADR-0036 intake).

    Returns (device_id, None) on success or (None, reason) if we can't. The record shape matches the
    hand-written ones exactly (key ``<node>-gas``, ``node_id`` back-reference, device_id ``gas_<area>``)
    so downstream — edge_mapper, viewmodel, relocate — cannot tell the difference.

    Only the gas lane is auto-registered. BLE relaying needs no record (the node forwards adverts keyed by
    the peripheral's MAC, which the mapper resolves against the registry independently), so a node with no
    gas ability is a legitimate relay-only node and is reported as such rather than half-registered.

    THIS PATH MUST NOT REQUIRE A HUMAN. It runs when an operator adopts hardware from the PWA, and the
    goal is a house that an operator runs alone. Anything it hands back that reads "go hand-edit the
    registry" is a design failure, not a helpful error — every branch below either completes the
    registration or explains a condition the SERVER genuinely cannot resolve (see the unknown-ability case).
    """
    import yaml

    from pathlib import Path

    # Read the registry FIRST — before the dry-run return. It used to be read after, so a preview computed
    # `gas_<area>` blind and reported a clean plan that the apply then rejected on collision (2026-08-02:
    # sgp41_mech previewed fine into mech_closet, then 400'd against the BME680 already holding
    # gas_mech_closet). A preview that cannot see the state it is previewing against is worse than none.
    path = Path(devices_path)
    raw = yaml.safe_load(path.read_text()) if path.exists() else {}
    raw = raw or {}
    # `devices:` with no entries parses to None, not {} — setdefault would hand back that None.
    devices = raw.get("devices") or {}
    raw["devices"] = devices

    key = f"{node_id}-gas"
    if key in devices:                       # already registered (re-adopt, or a race) — idempotent
        return (devices[key] or {}).get("device_id"), None

    gas = next((a for a in (abilities or []) if a in _GAS_CAPABILITIES), None)
    if not gas:
        # Distinguish "no gas lane" from "a gas lane this BUILD has never heard of". Both used to land on
        # the relay-only message below, which is stated as a verdict about the hardware — so a node that
        # was simply newer than the server read as a node that needed no record at all (2026-08-02:
        # ha-2 ran a pre-SGP41 _GAS_CAPABILITIES and told the operator his healthy sgp41_gas node was a
        # relay). An air-gapped prod box is exactly where the server trails the firmware, so name it.
        unknown = [a for a in (abilities or []) if a.endswith("_gas")]
        if unknown:
            return None, (f"node {node_id!r} announced gas ability {unknown} that THIS SERVER BUILD does "
                          f"not know (it knows {sorted(_GAS_CAPABILITIES)}). The node is fine — this "
                          f"server is behind it. Deploy the build that supports {unknown} and re-adopt; "
                          f"do not re-flash the node.")
        return None, (f"node {node_id!r} announced no gas ability ({list(abilities or []) or 'none'}) and "
                      f"has no registry record. A relay-only node needs no device record — it forwards "
                      f"BLE adverts keyed by the peripheral MAC. Nothing to adopt into a room.")

    device_id = _gas_device_id({(v or {}).get("device_id") for v in devices.values()}, area, gas)
    entry = {
        "device_id": device_id,
        "node_id": node_id,
        "device_type": gas,
        # Born DORMANT in `staging`, not in the target area — matching the standby_c6 precedent
        # ("air_quality stays dormant until a real area is assigned (one-line relocate)"). The caller
        # immediately relocates staging -> area, and THAT move is what wakes the ability. Registering
        # straight into the target area would make the relocate a same-area no-op, which the relocate
        # path rejects with a 400 (bench-found 2026-08-01). device_id is already the destination name so
        # the deferred gas_standby->gas_<area> rename never has to happen for auto-registered nodes.
        "area": DORMANT_AREA,
        "capabilities": list(_GAS_CAPABILITIES[gas]),
        "notes": (f"Auto-registered at ADR-0036 intake from node {node_id}'s self-described hello "
                  f"(abilities={list(abilities)}). Publishes home/edge/{node_id}/gas/adv; "
                  f"transport i2c-local."),
    }
    if dry_run:
        return device_id, None

    devices[key] = entry
    from server.device_registry import _write_yaml_preserving
    _write_yaml_preserving(path, raw)        # atomic + .bak + header-preserving, same as append_device
    return device_id, None


def handle_history_gaps(device_id: str, devices_path, db_path,
                        lookback_days: int = 3, min_gap_min: float = 20.0) -> tuple[int, dict]:
    """Read-only history checker. Scans the last ``lookback_days`` of ``hot.db`` for windows with no
    readings longer than ``min_gap_min`` and reports them, plus whether a recovery route exists for this
    device's type. Pure read — touches nothing."""
    import datetime
    import sqlite3
    gw = _gap_watcher()
    mac, info = _load_registry_device(devices_path, device_id)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        gaps, n = gw.device_gaps(conn, device_id, lookback_days, min_gap_min * 60.0)
        last = conn.execute("SELECT MAX(ts) FROM readings WHERE device_id=?", (device_id,)).fetchone()[0]
    finally:
        conn.close()

    def _iso(epoch):
        return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    plan = gw.backfill_plan(info, _backfill_routes()) if info else None
    out_gaps = [{"start": _iso(a), "end": _iso(b), "gap_min": round(s / 60.0, 1)} for a, b, s in gaps]
    return 200, {
        "status": "ok",
        "device_id": device_id,
        "known": info is not None,
        "lookback_days": lookback_days,
        "min_gap_min": min_gap_min,
        "reading_count": n,
        "last_reading": last,
        "gaps": out_gaps,
        "biggest_gap_min": max((g["gap_min"] for g in out_gaps), default=0),
        "recoverable": plan is not None,
        "recover_via": (plan or {}).get("via"),
    }


def handle_history_recover(device_id: str, devices_path, db_path,
                           body: dict[str, Any] | None = None) -> tuple[int, dict]:
    """Fire a type-appropriate history backfill for one device (edge GATT-pull / server pull / aranet).
    Fire-and-forget: the pull runs in a background thread and lands rows via the normal idempotent
    (INSERT OR IGNORE) path, so re-clicking is harmless. The real confirmation is re-running the checker
    and seeing the gap close — that's what the UI does."""
    import threading
    gw = _gap_watcher()
    mac, info = _load_registry_device(devices_path, device_id)
    if info is None or not mac:
        return 404, {"status": "unknown-device", "device_id": device_id,
                     "reason": "no registry entry (needs a MAC to pull history)"}
    plan = gw.backfill_plan(info, _backfill_routes())
    if not plan:
        return 200, {"status": "no_route", "device_id": device_id,
                     "device_type": info.get("device_type"),
                     "note": "no history-recovery route for this device type — its readings aren't "
                             "buffered on-device (e.g. gas/plug streams), so a gap can't be backfilled."}
    threading.Thread(target=gw.dispatch, args=(mac, info, plan, False), daemon=True).start()
    return 202, {
        "status": "launched",
        "device_id": device_id,
        "via": plan["via"],
        "node": plan.get("node"),
        "profile": plan.get("profile"),
        "note": "history pull started — re-check in ~30–60s to confirm the gap closed. "
                "Idempotent: safe to run again.",
    }


def handle_device_placement(device_id: str, body: dict[str, Any], placement_path) -> tuple[int, dict]:
    """Upsert a device's in-room placement for the spatial room-zoom (map arc 3). Body ``{x, y, anchor?}``:
    ``x,y`` normalized to the room bbox, a number in [0,1] or ``null`` (not captured); ``anchor`` one of
    n|s|e|w|auto (default auto). Writes ``device-placement.yaml`` (canonical on the dictator); the read path
    re-reads it each ``/rooms`` request so the panel picks it up live. Admin-gated; mirrors relocate/meta."""
    from server.maintenance.device_placement import write_placement
    b = body or {}
    x, y, anchor = b.get("x"), b.get("y"), b.get("anchor", "auto")

    def _coord_ok(v):
        return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= v <= 1.0)

    if not _coord_ok(x) or not _coord_ok(y):
        return 400, {"status": "bad-request", "reason": "x and y must be null or a number in [0,1]"}
    if anchor not in ("n", "s", "e", "w", "auto"):
        return 400, {"status": "bad-request", "reason": "anchor must be one of n,s,e,w,auto"}
    action = write_placement(placement_path, device_id, x, y, anchor)
    return 200, {"status": "ok", "device_id": device_id, "action": action,
                 "placement": {"x": x, "y": y, "anchor": anchor}}


def make_battery_router(master, node_secrets_lut, broker="localhost", port=1883):
    """On-demand SwitchBot battery refresh — POST /control/battery/refresh {device_id?}. Opens a brief
    MESH-WIDE active-scan window (same primitive as the nightly ha-battery-refresh sweep) so the tapped
    device's battery% updates within ~20s. Un-gated (LAN read-tier): it only triggers a scan window — no
    actuation, no config change. `device_id` is informational (any node that hears the meter may source it)."""
    from fastapi import APIRouter, Body
    from fastapi.responses import JSONResponse

    import paho.mqtt.client as mqtt

    from server.control.secret_store import load_lut
    from server.mesh import battery_refresh as br

    lut = load_lut(node_secrets_lut, master)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    try:
        client.connect(broker, port, 30)
        client.loop_start()
    except OSError:
        client = None   # broker unreachable at mount — endpoint 503s; app hides the button on error

    router = APIRouter(prefix="/control/battery", tags=["battery"])

    @router.post("/refresh")
    async def refresh(body: dict = Body(default={})):
        if client is None or not lut:
            return JSONResponse(status_code=503, content={
                "ok": False, "error": "battery refresh unavailable (no broker or node secret LUT)"})
        device_id = (body or {}).get("device_id")
        sent = br.sweep(lambda t, pl: client.publish(t, pl, qos=1), lut, window_s=20, stagger_s=0)
        return {"ok": True, "device_id": device_id, "nodes": sent, "window_s": 20,
                "note": "battery updates within ~20s as nodes finish their active-scan window"}

    return router


def make_device_meta_router(api_authz, control_db, placement_path=None, devices_path=None, db_path=None):
    """Admin-gated device overlay editor (prefix /api/v1/devices). Works for any device (sensors too).
    ``devices_path``/``db_path`` enable the history checker + recovery endpoints (registry MAC lookup +
    hot.db gap scan); omit them and those two routes are simply not mounted."""
    import os
    import sqlite3

    from fastapi import APIRouter, Body, Depends, Header, HTTPException
    from fastapi.responses import JSONResponse

    from server.control import control_store as store

    router = APIRouter(prefix="/api/v1/devices", tags=["devices"])

    def require_admin(authorization: str | None = Header(default=None)):
        if api_authz is None or not api_authz(authorization):
            raise HTTPException(status_code=401, detail="unauthorized",
                                headers={"WWW-Authenticate": "Bearer"})

    @router.put("/{device_id}/meta", dependencies=[Depends(require_admin)])
    async def put_meta(device_id: str, body: dict = Body(...)):
        c = sqlite3.connect(str(control_db))
        store.ensure_schema(c)
        try:
            code, payload = handle_device_meta(c, device_id, body)
        finally:
            c.close()
        return JSONResponse(status_code=code, content=payload)

    @router.post("/{device_id}/relocate", dependencies=[Depends(require_admin)])
    async def post_relocate(device_id: str, body: dict = Body(...)):
        from starlette.concurrency import run_in_threadpool
        try:
            code, payload = await run_in_threadpool(handle_device_relocate, device_id, body)
        except Exception as e:   # mutation may be partial — a per-op backup was written before any write
            return JSONResponse(status_code=500, content={
                "status": "error", "device_id": device_id, "reason": str(e),
                "note": "hot.db + registry were backed up to instance/db/backups/ before mutating; "
                        "see docs/runbook-dataset-restore.md"})
        return JSONResponse(status_code=code, content=payload)

    @router.post("/{device_id}/rename", dependencies=[Depends(require_admin)])
    async def post_rename(device_id: str, body: dict = Body(...)):
        from starlette.concurrency import run_in_threadpool
        try:
            code, payload = await run_in_threadpool(handle_device_rename, device_id, body)
        except Exception as e:   # mutation is detached; this only covers preview/launch failures
            return JSONResponse(status_code=500, content={
                "status": "error", "device_id": device_id, "reason": str(e),
                "note": "no mutation started; the underlying tools back up before any write — "
                        "see docs/runbook-dataset-restore.md"})
        return JSONResponse(status_code=code, content=payload)

    @router.get("/jobs/{job_id}", dependencies=[Depends(require_admin)])
    async def get_job(job_id: str):
        from server.maintenance import admin_job
        rec = admin_job.status(job_id)
        if rec is None:
            return JSONResponse(status_code=404, content={"status": "unknown", "job_id": job_id})
        return JSONResponse(status_code=200, content=rec)

    @router.put("/{device_id}/placement", dependencies=[Depends(require_admin)])
    async def put_placement(device_id: str, body: dict = Body(...)):
        if placement_path is None:
            return JSONResponse(status_code=503, content={"status": "unavailable",
                                                          "reason": "placement file not configured"})
        code, payload = handle_device_placement(device_id, body, placement_path)
        return JSONResponse(status_code=code, content=payload)

    @router.put("/{device_id}/calibration", dependencies=[Depends(require_admin)])
    async def put_calibration(device_id: str, body: dict = Body(...)):
        c = sqlite3.connect(str(control_db))
        store.ensure_schema(c)
        try:
            code, payload = handle_device_calibration(c, device_id, body)
        finally:
            c.close()
        return JSONResponse(status_code=code, content=payload)

    if devices_path is not None:
        _db = db_path or os.environ.get("HA_DB", "instance/db/hot.db")

        @router.get("/{device_id}/history/gaps", dependencies=[Depends(require_admin)])
        async def get_history_gaps(device_id: str, lookback_days: int = 3, min_gap_min: float = 20.0):
            from starlette.concurrency import run_in_threadpool
            code, payload = await run_in_threadpool(
                handle_history_gaps, device_id, devices_path, _db, lookback_days, min_gap_min)
            return JSONResponse(status_code=code, content=payload)

        @router.post("/{device_id}/history/recover", dependencies=[Depends(require_admin)])
        async def post_history_recover(device_id: str, body: dict = Body(default={})):
            code, payload = handle_history_recover(device_id, devices_path, _db, body)
            return JSONResponse(status_code=code, content=payload)

    @router.get("/meta")            # open read: hidden devices (to un-hide) + calibration offsets
    async def get_all_meta():
        c = sqlite3.connect(str(control_db))
        store.ensure_schema(c)
        try:
            return JSONResponse(status_code=200, content={"meta": store.all_device_meta(c),
                                                          "calibration": store.all_calibration(c)})
        finally:
            c.close()

    return router


def make_registry_router(api_authz, devices_path, control_path=None, node_secrets_path=None, master=None,
                         discovery_cache=None, edge_discovery_cache=None, broker="localhost", port=1883):
    """Admin-gated device registration (the add-device flow, ADR-0002 trait registry):
      GET  /api/v1/discover         -> unregistered BLE candidates heard nearby (the "see the filtered-out
                                       data" half of onboarding). Also returns edge_nodes[] (ADR-0036:
                                       online-but-unassigned edge NODES for standby-hardware intake) when
                                       edge_discovery_cache is given. Omitted unless a cache is provided.
      POST /api/v1/edge-nodes/{node}/intake -> adopt a standby edge node into a room (ADR-0036): relocate
                                       its dormant gas device (gas_standby) to `area`, waking air_quality.
      POST /api/v1/devices          -> append a SENSOR to devices.yaml
      POST /api/v1/control-devices  -> append an ACTUATOR to control.yaml (its command secret is derived
                                       from the owning node's enrolled cmd_secret). Omitted if control_path None.
      POST /api/v1/nodes            -> enrol a NEW node (mint cmd_secret, re-encrypt node_secrets.enc,
                                       return the secret + secrets.h). Omitted unless node_secrets_path+master.
    Separate from the control.db overlay router above."""
    from pathlib import Path

    from fastapi import APIRouter, Body, Depends, Header, HTTPException
    from fastapi.responses import JSONResponse

    from server.device_registry import handle_add_actuator, handle_add_device, handle_enroll_node, load_devices

    router = APIRouter(prefix="/api/v1", tags=["devices"])

    def require_admin(authorization: str | None = Header(default=None)):
        if api_authz is None or not api_authz(authorization):
            raise HTTPException(status_code=401, detail="unauthorized",
                                headers={"WWW-Authenticate": "Bearer"})

    if discovery_cache is not None or edge_discovery_cache is not None:
        @router.get("/discover", dependencies=[Depends(require_admin)])
        async def discover():
            """Two intake feeds: `candidates` = unregistered BLE devices the scanner heard (decoded model,
            temp/hum, battery; ranked by signal), already-registered MACs filtered out. `edge_nodes` =
            online-but-unassigned edge NODES that announced themselves (ADR-0036) — standby hardware
            awaiting a room. Either list is present only if its cache is wired."""
            resp: dict = {}
            if discovery_cache is not None:
                known = load_devices(Path(devices_path))
                resp["candidates"] = discovery_cache.candidates(known)
            if edge_discovery_cache is not None:
                resp["edge_nodes"] = edge_discovery_cache.candidates()
            return resp

    @router.post("/edge-nodes/{node}/intake", dependencies=[Depends(require_admin)])
    async def edge_node_intake(node: str, body: dict = Body(...)):
        """ADR-0036: adopt a standby edge node into a room. Relocates the node's dormant gas device
        (e.g. gas_standby) to `area`, waking its air_quality ability. Body {area, dry_run?=false}.
        Reuses the same relocate maintenance path as the per-device pencil menu, so intake can never
        drift from it. (device_id rename gas_standby->gas_<area> is a separate follow-up — the relocate
        is detached/async, so it isn't safe to chain a rename in the same request.)"""
        area = (body or {}).get("area")
        if not isinstance(area, str) or not area.strip():
            return JSONResponse(status_code=400,
                                content={"status": "bad-request", "reason": "area must be a non-empty string"})
        area = area.strip()
        dry_run = bool((body or {}).get("dry_run", False))
        mode = (body or {}).get("mode", "forward")

        # Resolve (and if needed CREATE) the node's device record BEFORE the claim below. Ordering is
        # deliberate: the claim burns the node's one-shot enroll window and cannot be undone, so nothing
        # that can still fail may come after it. (Bench-found 2026-08-01: the claim originally ran first,
        # so adopting brand-new hardware fired the irreversible claim and THEN returned 404 "no registered
        # device" — a permanent side effect reported to the operator as a failure.)
        device_id = _device_for_node(Path(devices_path), node)
        if not device_id:
            # Brand-new hardware: no hand-written registry record exists yet. ADR-0036's demo used
            # standby_c6, which had been registered at install time, so this path was never exercised —
            # every genuinely new node 404'd here. The node's own `hello` already carries everything the
            # record needs (node id + abilities), which is the whole point of a self-describing node, so
            # synthesise it rather than making the operator hand-edit devices.yaml.
            abilities = []
            if edge_discovery_cache is not None:
                abilities = (edge_discovery_cache.row(node) or {}).get("abilities") or []
            created, err = _register_edge_node_device(Path(devices_path), node, abilities, area,
                                                      dry_run=dry_run)
            if err:
                return JSONResponse(status_code=404,
                                    content={"status": "not-found", "node": node, "reason": err})
            device_id = created
            if dry_run:
                # A dry run writes no record, so there is nothing for handle_device_relocate to read a
                # CURRENT area from — it would fall back to device_last_seen and, for a node that has
                # already banked readings, report "target equals current" and 400 a perfectly valid
                # operation. Describe the two-step plan directly instead of previewing against a state
                # that doesn't exist yet.
                return JSONResponse(status_code=200, content={
                    "status": "preview", "node": node, "device_id": device_id, "area": area,
                    "dry_run": True, "claim": None,
                    "plan": [
                        {"step": "register", "key": f"{node}-gas", "device_id": device_id,
                         "device_type": next((a for a in abilities if a in _GAS_CAPABILITIES), None),
                         "area": DORMANT_AREA, "note": "new record, born dormant"},
                        {"step": "claim", "note": "TOFU claim of the node-born secret (skipped if already "
                                                  "enrolled)"},
                        {"step": "relocate", "from": DORMANT_AREA, "to": area,
                         "note": "wakes the air_quality ability"},
                    ]})

        # ADR-0036 Layer 3 — CLAIM before adopt. A node-born node (Layer 0) minted its own command secret
        # and nobody else knows it yet, so the dictator cannot sign anything to it: no OTA, no history
        # pull, no relay directive. Adopting it into a room without claiming would produce a half-wired
        # device that publishes readings but can never be commanded — the same half-wired-record failure
        # ADR-0036 was written to avoid. Claim first, and refuse the adopt if we can't.
        #
        # Skipped when the node is ALREADY in the LUT (every legacy build-time-enrolled node), so this is
        # a no-op for the existing fleet. Also skipped on dry_run — a dry run must not burn the node's
        # one-shot enroll window.
        claim_result = None
        if node_secrets_path is not None and master is not None and not dry_run:
            from server.control import edge_enroll
            from server.control import secret_store as ss
            try:
                already = node in ss.load_lut(Path(node_secrets_path), master)
            except Exception as exc:                      # noqa: BLE001 — LUT unreadable is operator-facing
                return JSONResponse(status_code=500,
                                    content={"status": "error", "reason": f"cannot read node LUT: {exc}"})
            if not already:
                try:
                    claim_result = edge_enroll.claim_node(
                        node, broker=broker, port=port, node_secrets_path=Path(node_secrets_path),
                        master=master)
                except edge_enroll.ClaimError as exc:
                    # Escape hatch for hardware that legitimately can't be claimed — pre-ADR-0036 firmware
                    # with no enroll handler, or a node whose one-shot window is already spent. Opt-in and
                    # LOUD, never a silent fallback: the resulting device publishes readings but cannot be
                    # commanded (no OTA, no history pull, no relay directive), and the operator should know
                    # they are adopting a half-wired node rather than discover it later.
                    if not bool((body or {}).get("allow_unclaimed", False)):
                        return JSONResponse(status_code=409,
                                            content={"status": "claim-failed", "node": node,
                                                     "reason": str(exc),
                                                     "hint": "re-POST with allow_unclaimed=true to adopt "
                                                             "anyway; the node will NOT accept commands "
                                                             "or OTA until it is enrolled"})
                    claim_result = {"status": "unclaimed", "node_id": node, "reason": str(exc),
                                    "warning": "adopted WITHOUT a command secret — node cannot be "
                                               "commanded or OTA'd"}
        # Intake default 'forward': the node's banked standby/bench data STAYS where it was (it isn't this
        # room's history); only readings from adoption onward are the room's. Override with mode=restamp to
        # carry the whole history to the new area. (`mode` and `device_id` are resolved above, before the
        # irreversible claim.)
        code, payload = handle_device_relocate(device_id, {"new_area": area, "mode": mode, "dry_run": dry_run})
        return JSONResponse(status_code=code,
                            content={"status": payload.get("status", "?"), "node": node,
                                     "device_id": device_id, "area": area, "dry_run": dry_run,
                                     "claim": claim_result, "relocate": payload})

    @router.post("/devices", dependencies=[Depends(require_admin)])
    async def add_device(body: dict = Body(...)):
        code, payload = handle_add_device(Path(devices_path), body)
        return JSONResponse(status_code=code, content=payload)

    if control_path is not None:
        @router.post("/control-devices", dependencies=[Depends(require_admin)])
        async def add_actuator(body: dict = Body(...)):
            code, payload = handle_add_actuator(Path(control_path), body)
            return JSONResponse(status_code=code, content=payload)

    if node_secrets_path is not None and master is not None:
        @router.post("/nodes", dependencies=[Depends(require_admin)])
        async def enroll_node(body: dict = Body(...)):
            code, payload = handle_enroll_node(Path(node_secrets_path), master, body)
            return JSONResponse(status_code=code, content=payload)

        @router.get("/flash/survey", dependencies=[Depends(require_admin)])
        async def flash_survey():
            """What is plugged into THIS box, who it already is, and what we can write to it.

            One read-only round trip so the UI is a confirmation surface: chip family, revision and MAC
            come off the silicon, so the operator confirms rather than types. `needs_rotate` flags a board
            that is already enrolled — flashing it would orphan a node the dictator can currently command.
            .210-only by design (Hugh: the one box with reliable USB port access)."""
            from server.maintenance import edge_flash as EF
            try:
                return JSONResponse(status_code=200, content=EF.survey(
                    node_secrets_path=node_secrets_path, master=master))
            except EF.FlashError as exc:
                return JSONResponse(status_code=500,
                                    content={"status": "error", "reason": str(exc)})

        @router.post("/flash", dependencies=[Depends(require_admin)])
        async def flash_board(body: dict = Body(...)):
            """Provision a board over USB. Detached — a flash takes ~40s, far past a sane request timeout —
            so this returns a job id and the caller polls /api/v1/devices/jobs/{id}, which streams `steps`.

            Body: {port, node_id, target?, gas_sensor?, broker_uri, wifi_ssid?, wifi_psk?, ntp_server?,
            ota_host?, erase?, confirm_rotate?}. The command secret is NOT accepted or produced here — it
            is node-born (ADR-0036 L0), so this path never handles a credential."""
            from server.maintenance import admin_job, edge_flash as EF
            b = dict(body or {})
            for required in ("port", "node_id"):
                if not str(b.get(required, "")).strip():
                    return JSONResponse(status_code=400,
                                        content={"status": "bad-request",
                                                 "reason": f"{required} is required"})
            # A network PROFILE (ssid+psk+broker+ota as one unit) is the supported way to say where the
            # node lives; a bare broker_uri is accepted only for CLI/test callers that set the SSID too.
            if not b.get("profile") and not b.get("broker_uri"):
                return JSONResponse(status_code=400, content={
                    "status": "bad-request",
                    "reason": "profile is required — pick the network the node should join. Broker and "
                              "Wi-Fi are not independent choices: the air-gap broker is only reachable "
                              "from the air-gap SSID."})
            try:                                   # resolve NOW so a mismatch fails at the click
                EF.apply_defaults({**b, "node_id": b["node_id"]})
            except EF.FlashError as exc:
                return JSONResponse(status_code=400,
                                    content={"status": "bad-request", "reason": str(exc)})
            if b.get("cmd_secret"):
                return JSONResponse(status_code=400, content={
                    "status": "bad-request",
                    "reason": "cmd_secret is not accepted — the node mints its own (ADR-0036 Layer 0) and "
                              "hands it over once at intake. Nothing should be sending one here."})
            # Validate cheap things NOW, synchronously, so obvious mistakes fail at the click instead of
            # 40 seconds later inside a detached job.
            gas = str(b.get("gas_sensor") or "auto")
            if gas not in EF.GAS_CHOICES:
                return JSONResponse(status_code=400, content={
                    "status": "bad-request",
                    "reason": f"gas_sensor must be one of {', '.join(EF.GAS_CHOICES)}"})
            b.setdefault("node_secrets_path", str(node_secrets_path))
            b.setdefault("master", master)
            job_id = admin_job.launch({"op": "flash", "flash": b})
            return JSONResponse(status_code=202, content={
                "status": "launched", "job_id": job_id,
                "poll": f"/api/v1/devices/jobs/{job_id}",
                "note": "the board reboots itself when done, mints its own secret, and announces hello — "
                        "then adopt it from Standby hardware"})

        @router.post("/edge-nodes/{node}/claim", dependencies=[Depends(require_admin)])
        async def claim_edge_node(node: str):
            """ADR-0036 Layer 3: claim a node-born node's secret WITHOUT adopting it into a room.

            The adopt flow (/intake) claims implicitly; this exists for the cases where you want the two
            separated — re-running a claim whose reply was lost, or enrolling bench hardware you aren't
            placing yet. Idempotent when the same node re-presents the same secret; a different secret for
            a known node_id is refused by the TOFU-lock rather than overwritten. Never returns the secret."""
            from server.control import edge_enroll
            try:
                return JSONResponse(status_code=200, content=edge_enroll.claim_node(
                    node, broker=broker, port=port, node_secrets_path=Path(node_secrets_path),
                    master=master))
            except edge_enroll.ClaimError as exc:
                return JSONResponse(status_code=409,
                                    content={"status": "claim-failed", "node": node, "reason": str(exc)})

    return router


def make_override_router(api_authz, control_db, device_ids=None):
    """Admin-gated control router (prefix /control): set/clear the manual override and read the live
    control state. Writes only control.db (the controller's source of truth); same bearer as /devices."""
    import sqlite3
    import time

    from fastapi import APIRouter, Body, Depends, Header, HTTPException
    from fastapi.responses import JSONResponse

    from server.control import control_store as store

    router = APIRouter(prefix="/control", tags=["control"])
    devices = set(device_ids) if device_ids is not None else None

    def require_admin(authorization: str | None = Header(default=None)):
        if api_authz is None or not api_authz(authorization):
            raise HTTPException(status_code=401, detail="unauthorized",
                                headers={"WWW-Authenticate": "Bearer"})

    def _conn():
        c = sqlite3.connect(str(control_db))
        store.ensure_schema(c)
        return c

    @router.get("/auth/check", dependencies=[Depends(require_admin)])
    async def auth_check():
        # 200 only if the admin bearer is valid (the dependency 401s otherwise). Lets the UI confirm a
        # password actually worked at login instead of discovering it on the first failed command.
        return JSONResponse(status_code=200, content={"ok": True})

    @router.post("/house/scene", dependencies=[Depends(require_admin)])
    async def post_house_scene(body: dict = Body(...)):
        # set the whole-house scene (Home/Away/Sleep). Two path segments after /control, so it never
        # collides with the single-segment /control/{device_id} route.
        c = _conn()
        try:
            code, payload = handle_set_scene(c, body)
        finally:
            c.close()
        return JSONResponse(status_code=code, content=payload)

    @router.get("/house/scene", dependencies=[Depends(require_admin)])
    async def get_house_scene():
        c = _conn()
        try:
            return JSONResponse(status_code=200,
                                content={**store.get_scene_full(c), "scenes": list(HOUSE_SCENES)})
        finally:
            c.close()

    @router.get("/house/night-mode", dependencies=[Depends(require_admin)])
    async def get_night_mode():
        c = _conn()
        try:
            return JSONResponse(status_code=200,
                                content={"night_mode": store.get_setting(c, "night_mode") or NIGHT_MODE_DEFAULT})
        finally:
            c.close()

    @router.put("/house/night-mode", dependencies=[Depends(require_admin)])
    async def put_night_mode(body: dict = Body(...)):
        c = _conn()
        try:
            code, payload = handle_set_night_mode(c, body)
        finally:
            c.close()
        return JSONResponse(status_code=code, content=payload)

    @router.post("/{device_id}/override", dependencies=[Depends(require_admin)])
    async def post_override(device_id: str, body: dict = Body(...)):
        c = _conn()
        try:
            code, payload = handle_override(c, device_id, body, time.time(), devices)
        finally:
            c.close()
        return JSONResponse(status_code=code, content=payload)

    @router.get("/{device_id}", dependencies=[Depends(require_admin)])
    async def get_control(device_id: str):
        if devices is not None and device_id not in devices:
            return JSONResponse(status_code=404, content={"status": "unknown-device"})
        c = _conn()
        try:
            state = read_control_state(c, device_id, time.time())
        finally:
            c.close()
        return JSONResponse(status_code=200, content=state)

    @router.put("/{device_id}/policy", dependencies=[Depends(require_admin)])
    async def put_policy(device_id: str, body: dict = Body(...)):
        c = _conn()
        try:
            code, payload = handle_policy_update(c, device_id, body, devices)
        finally:
            c.close()
        return JSONResponse(status_code=code, content=payload)

    return router


def make_router(issuer: CommandIssuer, confirm_verifier=None, api_authz=None):
    """Build the FastAPI control router (fastapi imported lazily so non-API code/tests don't need it).

    `api_authz(authorization_header) -> bool` is the admin gate (bearer = SHA256("ha-api:"+master), see
    secret_store.make_api_token_verifier). It is REQUIRED: with no verifier every request is 401, so the
    control plane can never be exposed unauthenticated even if mounted by mistake. `confirm_verifier`
    remains the SEPARATE second factor for sensitive actions (checked inside handle_command)."""
    from fastapi import APIRouter, Body, Depends, Header, HTTPException
    from fastapi.responses import JSONResponse

    router = APIRouter(prefix="/devices", tags=["control"])

    def require_admin(authorization: str | None = Header(default=None)):
        if api_authz is None or not api_authz(authorization):
            raise HTTPException(status_code=401, detail="unauthorized",
                                headers={"WWW-Authenticate": "Bearer"})

    # NB: take the JSON body as a `dict` param and return a JSONResponse rather than typed
    # Request/Response params — with `from __future__ import annotations` those local-scope types
    # aren't resolvable by FastAPI's hint analysis and get mis-read as query params (422).
    @router.post("/{device_id}/command", dependencies=[Depends(require_admin)])
    async def post_command(device_id: str, body: dict = Body(...)):
        # handle_command can block for seconds (a Midea LAN command shells out to the CLI, up to a 40s
        # subprocess timeout). Run it off the event loop so one device command doesn't stall the whole
        # async API (reads, other clients) while it waits on the appliance.
        from starlette.concurrency import run_in_threadpool
        code, payload = await run_in_threadpool(handle_command, issuer, device_id, body, confirm_verifier)
        return JSONResponse(status_code=code, content=payload)

    return router
