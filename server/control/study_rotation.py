"""Dehumidifier study, Phase 3 — the automatic rotation (docs/design/dehumidifier-efficiency-study.md §3D, §6c P3).

Natural operation mixes everything (units run together, weather moves at the same time), so the scheduler rotates
the levers in fixed blocks around the clock. Hugh approved the rotation 2026-11-12 after G2b passed.

    arm   Midea      Aprilaire (+ blower follows)   ERV
    0     off        off                            base (its air-quality bands; dry boost/trim/law held)
    M     on         off                            base
    A     off        on                             base
    MA    on         on                             base
    E     off        off                            boost (top level) — only while outdoor dew point is at least
                                                    e_min_gap_c below indoor; otherwise skipped this cycle

- Blocks of `block_min` (90) minutes; the analysis discards the first `settle_min` (30). One cycle = every eligible
  arm once, in a new random order each cycle.
- It drives the EXISTING override path (control.db `override`, the same rows a PWA override writes), each with an
  expiry of block end + `grace_min`: if the controller stops, the study's overrides lapse on their own and the
  production rules take back over.
- Arm E is opportunistic (Hugh 2026-11-12: outdoor air drier than indoor is rare in a wet season). A watcher
  samples the indoor − outdoor dew-point gap every `e_check_min` (10); after `e_confirm` (2) passing checks in a
  row, E goes next — ahead of the cycle's order — and a block still in its settle window is cut short
  (`preempt_e`, little data lost) and re-run right after E. A supervisor ends a running E block (`conditions`)
  once the gap has been below e_min_gap_c − `e_exit_hyst_c` for `e_confirm` checks. E never runs twice in a row,
  so each E block has a non-E neighbour under similar weather. Its blocks are collated later; the P3 gate
  doesn't wait for them.
- Guardrails end a block early (the early end is logged, and is itself data): living-room RH ≥ rh_high in an arm
  that isn't drying with both units → the next block is the cheapest drying arm (M, then MA); RH ≤ rh_low → the
  next block is 0; the Midea's bucket full ends an M/MA block, and Midea arms are skipped until it's emptied.
- An operator override on a study device (any PWA press) releases that device from the study until the block
  ends, and for every later block while that override is still active — the way a press releases the follow
  coupling. `paused` (the PWA's "Pause study") stops everything at once and hands every device back to its rules.
- Each block is a control.db `study_block` row: arm, planned/actual times, how it ended, conditions at the start.
  `l_per_kwh` stays NULL here; the readiness analysis computes it (method B needs minute data the controller
  doesn't keep).

Settings: house_setting `study:rotation` ({enabled, paused, ...DEFAULTS}); running state: `study:rotation:state`
(so a controller restart resumes the block it was in). The pure planning is `next_arm` / `guard`; `StudyRotation`
is the thin runner the controller calls once per tick, before the devices tick.
"""
from __future__ import annotations

import logging
import random
import time

log = logging.getLogger("ha.control.study")

ARMS = {                       # role -> override action for this arm (None = no override: the device's rule runs)
    "0": {"midea": "off", "aprilaire": "off", "erv": None},
    "M": {"midea": "boost_on", "aprilaire": "off", "erv": None},
    "A": {"midea": "off", "aprilaire": "boost_on", "erv": None},
    "MA": {"midea": "boost_on", "aprilaire": "boost_on", "erv": None},
    "E": {"midea": "off", "aprilaire": "off", "erv": "boost_on"},
}
MIDEA_ARMS = ("M", "MA")

DEFAULTS = {
    "enabled": False,
    "paused": False,
    "block_min": 90,
    "settle_min": 30,           # the analysis discards these; the scheduler only records it
    "rh_high": 48.0,            # Hugh 2026-10-31: house RH above 47–48 % ends a non-drying block
    "rh_low": 35.0,             # safety backstop only (never reached in the history)
    "e_min_gap_c": 2.0,         # arm E only while indoor − outdoor dew point ≥ this
    "grace_min": 10,            # override expiry past the block's end
    "e_check_min": 10,          # arm E watcher: sample the dew-point gap this often
    "e_confirm": 2,             # consecutive checks to start (gap ≥ e_min_gap_c) or stop (below it − hysteresis)
    "e_exit_hyst_c": 0.5,
    "rh_sensor": "meter_pro_living_room",
    "devices": {"midea": "dehumidifier_living_room", "aprilaire": "dehum_attic", "erv": "erv_attic"},
}
SETTING, STATE = "study:rotation", "study:rotation:state"

SCHEMA = """
CREATE TABLE IF NOT EXISTS study_block (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cycle INTEGER, arm TEXT NOT NULL,
    planned_start REAL, planned_end REAL, start_ts REAL, end_ts REAL, end_reason TEXT,
    rh_start REAL, rh_end REAL, inlet_dp_c REAL, dp_out_c REAL, t_out_c REAL,
    released TEXT, l_per_kwh REAL, note TEXT);
"""


def config(raw: dict | None) -> dict:
    raw = raw or {}
    cfg = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS and v is not None}}
    cfg["devices"] = {**DEFAULTS["devices"], **(raw.get("devices") or {})}
    return cfg


def new_order(rng: random.Random) -> list[str]:
    order = list(ARMS)
    rng.shuffle(order)
    return order


def next_arm(state: dict, rng: random.Random, e_ok: bool, bucket_full: bool) -> tuple[str, dict]:
    """(arm for the next block, updated state). A forced arm (from a guardrail) goes first and doesn't consume
    the cycle; otherwise the cycle's order advances, skipping E when it isn't eligible and the Midea arms while
    the bucket is full. A finished cycle starts a new random order."""
    st = dict(state)
    forced = st.pop("forced", None)
    if forced and not (forced in MIDEA_ARMS and bucket_full):
        return forced, st
    for _ in range(2 * len(ARMS) + 1):
        order, idx = st.get("order") or [], int(st.get("idx", 0))
        if idx >= len(order):
            st["cycle"] = int(st.get("cycle", 0)) + 1
            st["order"], st["idx"] = new_order(rng), 0
            continue
        arm = order[idx]
        st["idx"] = idx + 1
        if arm == "E" and not e_ok:
            continue
        if arm in MIDEA_ARMS and bucket_full:
            continue
        return arm, st
    return "0", st


def guard(arm: str, rh: float | None, bucket_full: bool, cfg: dict) -> tuple[str, str | None] | None:
    """(end_reason, forced next arm) when a guardrail ends the block early, else None. No RH reading → no
    RH guard (the units' own protections and the production thresholds' absence are bounded by the block)."""
    if bucket_full and arm in MIDEA_ARMS:
        return "bucket_full", None
    if rh is None:
        return None
    if rh >= float(cfg["rh_high"]) and arm != "MA":
        return "rh_high", ("MA" if arm == "M" else "M")
    if rh <= float(cfg["rh_low"]) and arm != "0":
        return "rh_low", "0"
    return None


def watch_e(w: dict | None, now: float, gap: float | None, cfg: dict) -> dict:
    """The arm-E watcher: every `e_check_min`, one sample of the dew-point gap. `ok` counts passing checks in a
    row (gap ≥ e_min_gap_c), `bad` failing ones (no reading, or gap below e_min_gap_c − e_exit_hyst_c); between
    the two thresholds neither counts, so a gap hovering at the line doesn't flap."""
    w = dict(w or {"last": None, "ok": 0, "bad": 0})
    if w.get("last") is not None and now - w["last"] < float(cfg["e_check_min"]) * 60:
        return w
    w["last"], w["gap"] = now, (round(gap, 2) if gap is not None else None)
    hi = float(cfg["e_min_gap_c"])
    if gap is not None and gap >= hi:
        w["ok"], w["bad"] = w.get("ok", 0) + 1, 0
    elif gap is None or gap < hi - float(cfg["e_exit_hyst_c"]):
        w["ok"], w["bad"] = 0, w.get("bad", 0) + 1
    return w


class StudyRotation:
    """Runs the rotation against control.db. `readings(cfg)` is a callable returning the conditions the scheduler
    needs: {"rh": living-room RH or None, "dp_in": °C or None, "dp_out": °C or None, "t_out": °C or None,
    "e_ok": bool, "bucket_full": bool}."""

    def __init__(self, store, rng: random.Random | None = None):
        self.store = store
        self.rng = rng or random.Random()
        self.arm: str | None = None             # the running arm, None = study inactive
        self.released: set[str] = set()
        self.devices: dict = dict(DEFAULTS["devices"])

    # ── helpers ────────────────────────────────────────────────────────────────
    def _save(self, conn, st: dict) -> None:
        self.store.set_setting(conn, STATE, st)

    @staticmethod
    def _raw_override(conn, device_id: str):
        r = conn.execute("SELECT action, expiry FROM override WHERE device_id=?", (device_id,)).fetchone()
        return (r[0], r[1]) if r else None

    def _clear_ours(self, conn, written: dict) -> None:
        for dev, (action, expiry) in (written or {}).items():
            if self._raw_override(conn, dev) == (action, expiry):
                self.store.clear_override(conn, dev)

    def _end_block(self, conn, st: dict, now: float, reason: str, rh: float | None) -> dict:
        blk = st.get("block")
        if blk:
            conn.execute("UPDATE study_block SET end_ts=?, end_reason=?, rh_end=?, released=? WHERE id=?",
                         (now, reason, rh, ",".join(sorted(set(blk.get("released", [])))) or None, blk["id"]))
            conn.commit()
            log.info("study block %s (arm %s) ended: %s", blk["id"], blk["arm"], reason)
        st = dict(st)
        st["block"] = None
        return st

    # ── the tick ───────────────────────────────────────────────────────────────
    def tick(self, conn, now: float, readings) -> str | None:
        """Advance the rotation; returns the running arm (None when inactive). Never raises into the controller."""
        try:
            return self._tick(conn, now, readings)
        except Exception:
            log.exception("study rotation tick failed")
            return self.arm

    def _tick(self, conn, now: float, readings) -> str | None:
        conn.executescript(SCHEMA)
        cfg = config(self.store.get_setting(conn, SETTING))
        self.devices = cfg["devices"]
        st = self.store.get_setting(conn, STATE) or {}
        blk = st.get("block")
        active = cfg["enabled"] and not cfg["paused"]
        if not active:
            if blk:                                  # paused/disabled mid-block: hand everything back
                self._clear_ours(conn, blk.get("written"))
                self._save(conn, self._end_block(conn, st, now, "paused" if cfg["enabled"] else "disabled", None))
            self.arm, self.released = None, set()
            return None
        r = readings(cfg) or {}
        devs = cfg["devices"]
        grace = float(cfg["grace_min"]) * 60
        st["e_watch"] = w = watch_e(st.get("e_watch"), now, r.get("dp_gap"), cfg)
        e_ready = w["ok"] >= int(cfg["e_confirm"])
        if blk:
            # a controller outage longer than the grace let the overrides lapse: that block isn't clean
            if now > blk["end"] + grace:
                st = self._end_block(conn, st, now, "gap", r.get("rh"))
                blk = None
            else:
                for dev, (action, expiry) in list((blk.get("written") or {}).items()):
                    if self._raw_override(conn, dev) != (action, expiry):      # someone else wrote it
                        blk.setdefault("released", []).append(dev)
                        del blk["written"][dev]
                        log.info("study: %s released (operator override) in block %s", dev, blk["id"])
                g = guard(blk["arm"], r.get("rh"), bool(r.get("bucket_full")), cfg)
                if g is None and blk["arm"] == "E" and w["bad"] >= int(cfg["e_confirm"]):
                    g = ("conditions", None)                # the supervisor: outdoor air no longer drier
                if (g is None and blk["arm"] != "E" and e_ready and blk.get("after") != "E"
                        and not blk.get("forced") and now - blk["start"] < float(cfg["settle_min"]) * 60):
                    g = ("preempt_e", None)                 # still settling: little lost, re-run after E
                    st["requeue"] = blk["arm"]
                if g is not None:
                    reason, forced = g
                    self._clear_ours(conn, blk.get("written"))
                    st = self._end_block(conn, st, now, reason, r.get("rh"))
                    if forced:
                        st["forced"] = forced
                    blk = None
                elif now >= blk["end"]:
                    st = self._end_block(conn, st, now, "complete", r.get("rh"))
                    blk = None
                else:
                    st["block"] = blk
                    self._save(conn, st)
                    self.arm, self.released = blk["arm"], set(blk.get("released", []))
                    return self.arm
        # start the next block
        prev_written = (st.get("last_written") or {})
        bucket = bool(r.get("bucket_full"))
        forced = bool(st.get("forced"))
        e_now = e_ready and st.get("prev_arm") != "E"       # never E twice in a row
        if not st.get("forced") and e_now:
            arm = "E"                                       # opportunistic: ahead of the cycle's order
        elif not st.get("forced") and st.get("requeue") and not (st["requeue"] in MIDEA_ARMS and bucket):
            arm = st.pop("requeue")
        else:
            arm, st = next_arm(st, self.rng, e_now, bucket)
        after, st["prev_arm"] = st.get("prev_arm"), arm      # `after`: the arm before this block (never cut after E)
        end = now + float(cfg["block_min"]) * 60
        cur = conn.execute(
            "INSERT INTO study_block(cycle, arm, planned_start, planned_end, start_ts, rh_start, inlet_dp_c, dp_out_c,"
            " t_out_c) VALUES(?,?,?,?,?,?,?,?,?)",
            (st.get("cycle"), arm, now, end, now, r.get("rh"), r.get("dp_in"), r.get("dp_out"), r.get("t_out")))
        conn.commit()
        written, released = {}, []
        for role, action in ARMS[arm].items():
            dev = devs.get(role)
            if not dev:
                continue
            row = self._raw_override(conn, dev)
            ours = prev_written.get(dev)
            foreign = (row is not None and row[0] != "clear" and (row[1] is None or row[1] > now)
                       and (ours is None or tuple(ours) != row))
            if foreign:                              # an operator override is still running: stay out of its way
                released.append(dev)
                continue
            if action is None:
                if ours is not None and row is not None and tuple(ours) == row:
                    self.store.clear_override(conn, dev)            # last block's study override, not this arm's
                continue
            exp = end + grace
            self.store.set_override(conn, dev, action, exp)
            written[dev] = (action, exp)
        blk = {"id": cur.lastrowid, "arm": arm, "start": now, "end": end, "written": written, "released": released,
               "forced": forced, "after": after}
        st["block"], st["last_written"] = blk, written
        self._save(conn, st)
        log.info("study block %s: arm %s until %s (cycle %s)%s", blk["id"], arm,
                 time.strftime("%H:%M:%SZ", time.gmtime(end)), st.get("cycle"),
                 f"; released {released}" if released else "")
        self.arm, self.released = arm, set(released)
        return arm

    def holds_ventilation(self, device_id: str) -> bool:
        """True while the study owns the ERV's level: every arm but E runs it at its air-quality base, so the
        dry boost, wet trim and dew-point law are held (study doc §7: "Phase 3 must own it")."""
        return self.arm is not None and device_id == self.devices.get("erv") and device_id not in self.released


def view(conn, store, now: float, limit: int = 12) -> dict:
    """The PWA's study card: settings, the running block, the recent blocks."""
    try:
        conn.executescript(SCHEMA)
    except Exception:
        pass
    cfg = config(store.get_setting(conn, SETTING))
    st = store.get_setting(conn, STATE) or {}
    blk = st.get("block")
    rows = conn.execute("SELECT id, cycle, arm, start_ts, end_ts, end_reason, rh_start, rh_end, inlet_dp_c, released "
                        "FROM study_block ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    counts = dict(conn.execute("SELECT arm, COUNT(*) FROM study_block WHERE end_reason='complete' GROUP BY arm"))
    return {
        "enabled": cfg["enabled"], "paused": cfg["paused"], "block_min": cfg["block_min"],
        "rh_high": cfg["rh_high"], "rh_low": cfg["rh_low"],
        "block": ({"id": blk["id"], "arm": blk["arm"], "start": blk["start"], "end": blk["end"],
                   "left_s": max(0, blk["end"] - now), "released": blk.get("released", [])} if blk else None),
        "cycle": st.get("cycle"), "complete": {a: counts.get(a, 0) for a in ARMS},
        "e_watch": st.get("e_watch"),
        "recent": [{"id": r[0], "cycle": r[1], "arm": r[2], "start": r[3], "end": r[4], "end_reason": r[5],
                    "rh_start": r[6], "rh_end": r[7], "inlet_dp_c": r[8], "released": r[9]} for r in rows],
    }


def set_config(conn, store, body: dict) -> tuple[int, dict]:
    """PUT /control/house/study: {enabled?, paused?} (+ the guard numbers). Pure apart from the setting write."""
    b = body or {}
    cur = config(store.get_setting(conn, SETTING))
    for k in ("enabled", "paused"):
        if k in b and not isinstance(b[k], bool):
            return 400, {"status": "bad-request", "reason": f"{k} must be a boolean"}
    rng = {"block_min": (30, 360), "rh_high": (40, 60), "rh_low": (25, 45), "e_min_gap_c": (0.5, 6),
           "e_check_min": (1, 60), "e_confirm": (1, 6), "e_exit_hyst_c": (0, 2)}
    for k, (lo, hi) in rng.items():
        if k in b and (not isinstance(b[k], (int, float)) or isinstance(b[k], bool) or not lo <= b[k] <= hi):
            return 400, {"status": "bad-request", "reason": f"{k} must be a number in {lo}–{hi}"}
    new = {**cur, **{k: b[k] for k in ("enabled", "paused", *rng) if k in b}}
    if new["rh_low"] >= new["rh_high"]:
        return 400, {"status": "bad-request", "reason": "rh_low must be below rh_high"}
    store.set_setting(conn, SETTING, {k: new[k] for k in DEFAULTS if k != "devices"} | {"devices": cur["devices"]})
    return 200, {"status": "ok", "study": {k: new[k] for k in ("enabled", "paused", *rng)}}

