"""ADR-0015 Phase B — persisted adv-reception RATE signal (server/mesh/store.py).

A source heard steadily must end up with a higher decayed adv_score than one heard sporadically, so the
coordinator (which reads the persisted graph, unlike the live real-time Assigner) can tell 'steady' from
'lucky-recent'. Pulls (ok True/False) must NOT inflate the adv rate.
"""
import sqlite3
import time

from server.mesh import store as ms

EP = ("endpoint", "m1")


def _conn():
    c = sqlite3.connect(":memory:")
    ms.ensure_schema(c)
    return c


def _ts(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _score(conn, src) -> float:
    return conn.execute("SELECT adv_score FROM mesh_links WHERE src_id=? AND dst_id=?",
                        (src[1], EP[1])).fetchone()[0]


def test_steady_source_outscores_gappy_source():
    conn = _conn()
    base = 1_000_000.0
    # steady: heard every 30s for 5 min; gappy: heard every 5 min over the same span
    for i in range(10):
        ms.record_link(conn, ("node", "steady"), EP, "ble-adv", rssi=-88, ok=None, ts=_ts(base + i * 30))
    for i in range(2):
        ms.record_link(conn, ("node", "gappy"), EP, "ble-adv", rssi=-70, ok=None, ts=_ts(base + i * 300))
    assert _score(conn, ("node", "steady")) > 3 * _score(conn, ("node", "gappy"))


def test_pull_outcomes_do_not_inflate_adv_rate():
    conn = _conn()
    base = 1_000_000.0
    # only GATT pulls recorded (ok True/False) — adv_score must stay 0 (these aren't adv receptions)
    ms.record_link(conn, ("node", "puller"), EP, "ble-gatt", rssi=-60, ok=True, ts=_ts(base))
    ms.record_link(conn, ("node", "puller"), EP, "ble-gatt", rssi=-60, ok=False, ts=_ts(base + 30))
    assert _score(conn, ("node", "puller")) == 0.0


def test_load_links_returns_decayed_rate_steady_gt_gappy():
    conn = _conn()
    base = 1_000_000.0
    for i in range(10):
        ms.record_link(conn, ("node", "steady"), EP, "ble-adv", rssi=-88, ok=None, ts=_ts(base + i * 30))
    ms.record_link(conn, ("node", "gappy"), EP, "ble-adv", rssi=-70, ok=None, ts=_ts(base + 270))
    now = base + 300  # 30s after the last steady sighting
    by_src = {l.src[1]: l for l in ms.load_links(conn, now=now)}
    assert by_src["steady"].rate is not None and by_src["steady"].rate > by_src["gappy"].rate
