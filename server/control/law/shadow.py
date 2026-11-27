"""The unified law's runner (docs/design/unified-control-law.md §1.7, stages L2 + L4): every `INTERVAL_S` the
controller hands it the house's state; it solves, holds each device's answer at least `MIN_HOLD_S` (the same reason
the rules have min on/off times), and logs what it decided next to what the devices did, in control.db `law_shadow`.

This module never commands anything itself. Per device (`law:enrol`):
  rule    the device's own automation runs; left out of the comparison
  shadow  (default) its own automation runs; the law's answer is logged next to it
  law     the controller drives the device with `decision()` (controller._apply_law) below overrides, scenes,
          schedules and safety; the row logs what its own rule would have done instead (`rule` column)
"""
from __future__ import annotations

import json
import logging
import time

from server.control.law import bands as B
from server.control.law import models as M
from server.control.law import solve as S

log = logging.getLogger("ha.control.law")

INTERVAL_S = 300
STARTUP_S = 120
MIN_HOLD_S = 900
STALE_S = 3 * INTERVAL_S            # a law-driven device older than this falls back to its own rule
KEEP_DAYS = 60
SCHEMA = """
CREATE TABLE IF NOT EXISTS law_shadow (
    ts REAL NOT NULL, plan TEXT, actual TEXT, errors TEXT, need_gph REAL, totals TEXT, reasons TEXT, house TEXT);
CREATE INDEX IF NOT EXISTS idx_law_shadow_ts ON law_shadow(ts);
"""


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(law_shadow)")}
    if "rule" not in cols:                   # L4: what a law-driven device's own rule would have done
        conn.execute("ALTER TABLE law_shadow ADD COLUMN rule TEXT")


class Shadow:
    def __init__(self, store, now: float | None = None):
        self.store = store
        # first decision 2 min after start: device telemetry arrives over the first ticks, and a decision made
        # before it would compare against "unknown"
        self.last_run = (time.time() if now is None else now) - INTERVAL_S + STARTUP_S
        self.held: dict[str, tuple[int | None, float, str]] = {}    # device -> (level, since, label)
        self.choice: dict[str, dict] = {}                           # the last decision, per device
        self.decided_at: float | None = None                        # when it was made (None = not yet)
        self.rule_said: dict[str, int | None] = {}                  # law-driven devices: their rule's level

    def decision(self, device_id: str, now: float) -> dict | None:
        """The law's current answer for a device ({level, label, …}), or None when there is no fresh one (not
        decided yet, the last tick failed for longer than STALE_S, or the device isn't in the plan)."""
        if self.decided_at is None or now - self.decided_at > STALE_S:
            return None
        return self.choice.get(device_id)

    def due(self, now: float) -> bool:
        return now - self.last_run >= INTERVAL_S

    def run(self, conn, now: float, registry: dict, house: S.House, current: dict,
            study_arm: str | None = None) -> S.Plan | None:
        """One shadow decision. `current` = {device_id: level now (0 = off)}; `study_arm` = the dehumidifier study's
        running arm, if any (then "now" is the experiment, not the rules). Never raises into the controller."""
        self.last_run = now
        try:
            return self._run(conn, now, registry, house, current, study_arm)
        except Exception:
            log.exception("law shadow tick failed")
            return None

    def _run(self, conn, now, registry, house, current, study_arm=None):
        ensure_schema(conn)
        st = B.get(conn, self.store)
        prm = M.params(self.store.get_setting(conn, "law:params"))
        models = M.build(registry, prm, {"w_in": house.w_in, "w_out": house.w_out, "t_in": house.t_in_c,
                                         "t_out": house.t_out_c})
        # the law's hysteresis (drying to the band's middle, outdoor drying's restart) runs on its OWN last answers,
        # as it would when enrolled; on the rules' levels it chattered at the floor (11-20: the rule's ERV high
        # held the law's drying on at 41 %). The comparison below still uses `current`.
        own = {**current, **{d: v[0] for d, v in self.held.items() if v[0] is not None}}
        bands, sched = B.effective_now(conn, self.store, now, st)     # the goals in force now (goal schedules)
        plan = S.solve(models, bands, st["cost"], prm, house, own)
        if sched:
            plan.reasons.insert(0, f"goals: the {sched} schedule")
        choice = {}
        for did, v in plan.choice.items():                  # min hold, per device, on the law's own answers
            lvl, since, label = self.held.get(did, (None, 0.0, ""))
            if lvl is not None and v["level"] != lvl and now - since < MIN_HOLD_S:
                choice[did] = {**v, "level": lvl, "label": label, "held_s": round(MIN_HOLD_S - (now - since))}
                continue
            if v["level"] != lvl:
                self.held[did] = (v["level"], now, v["label"])
            choice[did] = v
        self.choice, self.decided_at = choice, now
        shadowed = {d: c for d, c in choice.items() if B.enrolment(st, d) in ("shadow", "law")}
        actual = {d: current.get(d) for d in shadowed}
        rule = {d: self.rule_said.get(d) for d in shadowed if B.enrolment(st, d) == "law"}
        conn.execute("INSERT INTO law_shadow(ts, plan, actual, errors, need_gph, totals, reasons, house, rule) "
                     "VALUES(?,?,?,?,?,?,?,?,?)",
                     (now, json.dumps(shadowed), json.dumps(actual), json.dumps(plan.errors), plan.need_gph,
                      json.dumps(plan.totals), json.dumps(plan.reasons),
                      json.dumps({**{k: getattr(house, k) for k in ("t_in_c", "rh", "t_rh_c", "w_out", "t_out_c",
                                                                    "aq", "heating", "cooling")},
                                  "study_arm": study_arm}), json.dumps(rule)))
        conn.execute("DELETE FROM law_shadow WHERE ts < ?", (now - KEEP_DAYS * 86400,))
        conn.commit()
        self.store.set_setting(conn, "law:last", {"ts": now, "choice": choice, "actual": actual, "rule": rule,
                                                  "errors": plan.errors, "need_gph": plan.need_gph,
                                                  "totals": plan.totals, "reasons": plan.reasons,
                                                  "alternatives": plan.alternatives, "study_arm": study_arm,
                                                  "schedule": sched, "bands": {k: bands[k] for k in B.SCHEDULE_GOALS},
                                                  "models": M.inventory(models)})
        log.info("law%s: %s", " (driving " + ", ".join(sorted(rule)) + ")" if rule else " shadow",
                 "; ".join(plan.reasons))
        return plan


def view(conn, store, now: float) -> dict:
    """GET /api/v1/law: settings, the latest decision (with the model table), and 24 h of agreement with the
    rules per device."""
    try:
        ensure_schema(conn)
    except Exception:
        pass
    st = B.get(conn, store)
    last = store.get_setting(conn, "law:last")
    agree, rule_agree, study_ticks = {}, {}, 0
    for plan, actual, house, rule in conn.execute("SELECT plan, actual, house, rule FROM law_shadow WHERE ts > ?",
                                                  (now - 86400,)):
        if (json.loads(house or "{}") or {}).get("study_arm"):
            study_ticks += 1                     # "now" was the study's experiment, not the rules: not compared
            continue
        p, a, r = json.loads(plan or "{}"), json.loads(actual or "{}"), json.loads(rule or "{}")
        for d, v in p.items():
            if v.get("level") is None:
                continue
            n, same = agree.get(d, (0, 0))
            agree[d] = (n + 1, same + (1 if a.get(d) == v["level"] else 0))
            if d in r and r[d] is not None:      # law-driven: would its own rule have chosen the same?
                n, same = rule_agree.get(d, (0, 0))
                rule_agree[d] = (n + 1, same + (1 if r[d] == v["level"] else 0))
    from server.control import manual_restore as MR
    from server.control.law import holds as H
    from server.control.law import modes as MO
    devs = [m["device_id"] for m in ((last or {}).get("models") or [])]
    eff, sched = B.effective_now(conn, store, now, st)
    nxt = B.next_change(st["bands"], now, store.get_setting(conn, "timezone"))
    return {**st, "enrol_modes": list(B.ENROL_MODES), "params": M.params(store.get_setting(conn, "law:params")),
            "modes": {d: MO.of(conn, store, d, st) for d in devs},
            "has_policy": {d: store.get_policy(conn, d) is not None for d in devs},
            "holds": H.all_active(conn, now),
            "thermostats": {d: store.get_setting(conn, "law:thermo:" + d) for d in devs
                            if store.get_setting(conn, "law:thermo:" + d)},
            "manual_memory": {d: store.get_setting(conn, MR.key(d)) for d in devs
                              if store.get_setting(conn, MR.key(d))},
            "goals_now": {"schedule": sched, "bands": {k: eff[k] for k in B.SCHEDULE_GOALS}, "next_change": nxt},
            "last": last, "agreement_24h": {d: {"ticks": n, "same": s} for d, (n, s) in agree.items()},
            "rule_agreement_24h": {d: {"ticks": n, "same": s} for d, (n, s) in rule_agree.items()},
            "study_ticks_24h": study_ticks,
            "stale": last is None or now - last["ts"] > 3 * INTERVAL_S}
