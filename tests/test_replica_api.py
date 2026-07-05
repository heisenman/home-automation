"""Instance-replica lane (#7, docs/design/instance-replica-lane.md): the /api/v1/replica/{manifest.json,file}
routes the panel's ha_replica files-lane consumes. Verifies the manifest shape + sha256, the source-of-record
gate, the config ALLOWLIST (secrets never offered/served), the parquet cap + traversal rejection, and the
hot.db consistent snapshot. Routes are called directly against a synthetic instance/ tree (mirrors
test_rung_api — no lifespan/TestClient). `tmp_path` is provided by tests/_harness."""
import hashlib
import os
import sqlite3
from pathlib import Path

import server.api.main as M
from tests._harness import raises


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _mk_instance(root: Path) -> Path:
    """A synthetic data-of-record tree: allowlisted config + two secret files (must never surface) + 3 parquet
    day-files (distinct mtimes) + a real hot.db."""
    inst = root / "instance"
    inst.mkdir()
    (inst / "control.yaml").write_text("devices: {}\n")
    (inst / "areas.yaml").write_text("areas: []\n")
    (inst / "devices.yaml").write_text("d: 1\n")
    (inst / "control_secrets.yaml").write_text("token: SUPERSECRET\n")   # must NOT be offered/served
    (inst / "node_secrets.enc").write_bytes(b"\x00binblob")              # must NOT be offered/served
    pq = inst / "db" / "parquet" / "year=2026" / "month=07"
    pq.mkdir(parents=True)
    for i, day in enumerate(("01", "02", "03")):
        f = pq / f"2026-07-{day}.parquet"
        f.write_bytes(f"parquet-{day}".encode())
        os.utime(f, (1_700_000_000 + i, 1_700_000_000 + i))             # strictly increasing mtime: 03 newest
    dbp = inst / "db" / "hot.db"
    c = sqlite3.connect(str(dbp))
    c.execute("CREATE TABLE r(x INTEGER)")
    c.execute("INSERT INTO r VALUES (42)")
    c.commit()
    c.close()
    return inst


def _install(root: Path, sor=True, parquet_max=30):
    """Point the module globals at the fixture; return (inst, restore). Forces the source-of-record gate
    (the real gate = vip_held, which only answers True on the VIP-holding dictator)."""
    inst = _mk_instance(root)
    saved = (M.INSTANCE_DIR, M.PARQUET_GLOB, M.DB_PATH, M.REPLICA_PARQUET_MAX,
             M._replica_is_source_of_record, M._replica_sha_cache)
    M.INSTANCE_DIR = inst
    M.PARQUET_GLOB = inst / "db" / "parquet"
    M.DB_PATH = inst / "db" / "hot.db"
    M.REPLICA_PARQUET_MAX = parquet_max
    M._replica_is_source_of_record = lambda: sor
    M._replica_sha_cache = {}

    def restore():
        (M.INSTANCE_DIR, M.PARQUET_GLOB, M.DB_PATH, M.REPLICA_PARQUET_MAX,
         M._replica_is_source_of_record, M._replica_sha_cache) = saved
    return inst, restore


# ── manifest: shape, sha, allowlist (no secrets), source-of-record gate ─────────────────────────────────
def test_manifest_lists_config_parquet_hotdb_by_sha(tmp_path):
    inst, restore = _install(tmp_path)
    try:
        m = M.replica_manifest()
        assert m["is_source_of_record"] is True
        assert m["source_tag"] and m["generated_ts"].endswith("Z")
        kinds = {a["kind"] for a in m["artifacts"]}
        assert kinds == {"config", "parquet", "hotdb"}
        names = {a["name"] for a in m["artifacts"]}
        # allowlisted config present; secrets absent
        assert {"control.yaml", "areas.yaml", "devices.yaml"} <= names
        assert "control_secrets.yaml" not in names and "node_secrets.enc" not in names
        # sha matches the real file bytes (config + hotdb)
        cfg = next(a for a in m["artifacts"] if a["name"] == "control.yaml")
        assert cfg["sha256"] == _sha(inst / "control.yaml")
        assert cfg["size"] == (inst / "control.yaml").stat().st_size
        hot = next(a for a in m["artifacts"] if a["kind"] == "hotdb")
        assert hot["name"] == "hot.db" and hot["sha256"] == _sha(inst / "db" / "hot.db")
        # parquet names are relative to the parquet dir
        pq = next(a for a in m["artifacts"] if a["kind"] == "parquet")
        assert pq["name"].startswith("year=2026/month=07/") and pq["name"].endswith(".parquet")
    finally:
        restore()


def test_manifest_source_of_record_false_is_reported(tmp_path):
    _, restore = _install(tmp_path, sor=False)
    try:
        m = M.replica_manifest()
        assert m["is_source_of_record"] is False   # panel treats this as "keep the good copy, don't overwrite"
    finally:
        restore()


def test_manifest_parquet_cap_keeps_newest(tmp_path):
    _, restore = _install(tmp_path, parquet_max=2)
    try:
        m = M.replica_manifest()
        pqs = [a["name"] for a in m["artifacts"] if a["kind"] == "parquet"]
        assert len(pqs) == 2                                        # capped
        assert any("2026-07-03" in n for n in pqs) and any("2026-07-02" in n for n in pqs)
        assert not any("2026-07-01" in n for n in pqs)              # oldest dropped
    finally:
        restore()


# ── file: allowlist enforcement, traversal rejection, hot.db snapshot ───────────────────────────────────
def test_file_serves_allowlisted_config(tmp_path):
    inst, restore = _install(tmp_path)
    try:
        resp = M.replica_file(kind="config", name="control.yaml")
        assert Path(resp.path).read_text() == (inst / "control.yaml").read_text()
    finally:
        restore()


def test_file_refuses_secret_config(tmp_path):
    _, restore = _install(tmp_path)
    try:
        with raises(M.HTTPException):
            M.replica_file(kind="config", name="control_secrets.yaml")   # not on the allowlist → 404
        with raises(M.HTTPException):
            M.replica_file(kind="config", name="node_secrets.enc")
    finally:
        restore()


def test_file_rejects_parquet_traversal(tmp_path):
    _, restore = _install(tmp_path)
    try:
        with raises(M.HTTPException):
            M.replica_file(kind="parquet", name="../../../control_secrets.yaml")
        with raises(M.HTTPException):
            M.replica_file(kind="parquet", name="../../control.yaml")
        # a legit parquet still serves
        resp = M.replica_file(kind="parquet", name="year=2026/month=07/2026-07-03.parquet")
        assert Path(resp.path).read_bytes() == b"parquet-03"
    finally:
        restore()


def test_file_hotdb_is_a_consistent_snapshot(tmp_path):
    _, restore = _install(tmp_path)
    try:
        resp = M.replica_file(kind="hotdb", name="hot.db")
        snap = Path(resp.path)
        try:
            c = sqlite3.connect(str(snap))
            assert c.execute("SELECT x FROM r").fetchone()[0] == 42   # snapshot is a valid, complete DB
            c.close()
        finally:
            if snap.exists():
                snap.unlink()   # in prod a BackgroundTask deletes it post-send
    finally:
        restore()


def test_file_bad_kind_400(tmp_path):
    _, restore = _install(tmp_path)
    try:
        with raises(M.HTTPException):
            M.replica_file(kind="nonsense", name="x")
    finally:
        restore()
