"""The unified law in shadow (docs/design/unified-control-law.md §1.7, stage L2): every `INTERVAL_S` the controller
hands it the house's state; it solves, holds each device's answer at least `MIN_HOLD_S` (the same reason the rules
have min on/off times), and logs what it would do next to what the rules did, in control.db `law_shadow`.

It never commands anything. Enrolment `rule` leaves a device out of the comparison; `shadow` (the default) logs it.
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
MIN_HOLD_S = 900
KEEP_DAYS = 60
SCHEMA = """
CREATE TABLE IF NOT EXISTS law_shadow (
    ts REAL NOT NULL, plan TEXT, actual TEXT, errors TEXT, need_gph REAL, totals TEXT, reasons TEXT, house TEXT);
CREATE INDEX IF NOT EXISTS idx_law_shadow_ts ON law_shadow(ts);
"""


class Shadow:
    def __init__(self, store):
        self.store = store
        self.last_run = 0.0
        self.held: dict[str, tuple[int | None, float]] = {}    # device -> (level, since)

    def due(self, now: float) -> bool:
        return now - self.last_run >= INTERVAL_S

    def run(self, conn, now: float, registry: dict, house: S.House, current: dict) -> S.Plan | None:
        """One shadow decision. `current` = {device_id: level now (0 = off)}. Never raises into the controller."""
        self.last_run = now
        try:
            return self._run(conn, now, registry, house, current)
        except Exception:
            log.exception("law shadow tick failed")
            return None

    def _run(self, conn, now, registry, house, current):
        conn.executescript(SCHEMA)
        st = B.get(conn, self.store)
        prm = M.params(self.store.get_setting(conn, "law:params"))
        models = M.build(registry, prm, {"w_in": house.w_in, "w_out": house.w_out, "t_in": house.t_in_c,
                                         "t_out": house.t_out_c})
        plan = S.solve(models, st["bands"], st["cost"], prm, house, current)
        choice = {}
        for did, v in plan.choice.items():                  # min hold, per device, on the law's own answers
            lvl, since = self.held.get(did, (None, 0.0))
            if lvl is not None and v["level"] != lvl and now - since < MIN_HOLD_S:
                choice[did] = {**v, "level": lvl, "held_s": round(MIN_HOLD_S - (now - since))}
                continue
            if v["level"] != lvl:
                self.held[did] = (v["level"], now)
            choice[did] = v
        shadowed = {d: c for d, c in choice.items() if B.enrolment(st, d) == "shadow"}
        actual = {d: current.get(d) for d in shadowed}
        conn.execute("INSERT INTO law_shadow(ts, plan, actual, errors, need_gph, totals, reasons, house) "
                     "VALUES(?,?,?,?,?,?,?,?)",
                     (now, json.dumps(shadowed), json.dumps(actual), json.dumps(plan.errors), plan.need_gph,
                      json.dumps(plan.totals), json.dumps(plan.reasons),
                      json.dumps({k: getattr(house, k) for k in ("t_in_c", "rh", "t_rh_c", "w_out", "t_out_c", "aq",
                                                                 "heating", "cooling")})))
        conn.execute("DELETE FROM law_shadow WHERE ts < ?", (now - KEEP_DAYS * 86400,))
        conn.commit()
        self.store.set_setting(conn, "law:last", {"ts": now, "choice": choice, "actual": actual,
                                                  "errors": plan.errors, "need_gph": plan.need_gph,
                                                  "totals": plan.totals, "reasons": plan.reasons,
                                                  "alternatives": plan.alternatives,
                                                  "models": M.inventory(models)})
        log.info("law shadow: %s", "; ".join(plan.reasons))
        return plan


def view(conn, store, now: float) -> dict:
    """GET /api/v1/law: settings, the latest decision (with the model table), and 24 h of agreement with the
    rules per device."""
    try:
        conn.executescript(SCHEMA)
    except Exception:
        pass
    st = B.get(conn, store)
    last = store.get_setting(conn, "law:last")
    agree = {}
    for plan, actual in conn.execute("SELECT plan, actual FROM law_shadow WHERE ts > ?", (now - 86400,)):
        p, a = json.loads(plan or "{}"), json.loads(actual or "{}")
        for d, v in p.items():
            if v.get("level") is None:
                continue
            n, same = agree.get(d, (0, 0))
            agree[d] = (n + 1, same + (1 if a.get(d) == v["level"] else 0))
    return {**st, "enrol_modes": list(B.ENROL_MODES), "params": M.params(store.get_setting(conn, "law:params")),
            "last": last, "agreement_24h": {d: {"ticks": n, "same": s} for d, (n, s) in agree.items()},
            "stale": last is None or now - last["ts"] > 3 * INTERVAL_S}
