"""ESPHome purifier intake: discovery (server/ingest/esphome_discovery.py) + adoption
(server/esphome_intake.py) + the Levoit bridge's wildcard subscriptions.

The gap this closes (2026-10-01, levoit-c-office): a reflashed Levoit was online and publishing, but the
bridge only subscribed to ALREADY-registered names and the standby list only knew edge `hello`s — so a new
purifier was invisible to every intake surface and its data reached nobody.
"""
import os
import textwrap

import pytest

yaml = pytest.importorskip("yaml")

from server import esphome_intake as EI  # noqa: E402
from server.ingest import esphome_discovery as D  # noqa: E402
from server.ingest import levoit_bridge as LB  # noqa: E402


# ── discovery ─────────────────────────────────────────────────────────────────────────────────────

def _purifier(cache, name="levoit-c-office", now=100.0, online="online"):
    cache.ingest(f"{name}/status", online, now=now)
    cache.ingest(f"{name}/fan/fan/state", "OFF", now=now)
    cache.ingest(f"{name}/sensor/pm_2_5/state", "12", now=now)


def test_purifier_classified_by_what_it_publishes():
    c = D.EsphomeDiscoveryCache()
    _purifier(c)
    [row] = c.candidates(registered=set(), now=101.0)
    assert row["node"] == "levoit-c-office"
    assert row["abilities"] == ["air_purifier"] and row["kind"] == "esphome"


def test_partial_signature_is_not_a_purifier():
    """A panel publishing status + a PM2.5 reading but no fan is not adoptable as a purifier."""
    c = D.EsphomeDiscoveryCache()
    c.ingest("e1001/status", "online", now=1.0)
    c.ingest("e1001/sensor/pm_2_5/state", "3", now=1.0)
    assert c.candidates(registered=set(), now=2.0) == []


def test_registered_and_offline_nodes_are_not_candidates():
    c = D.EsphomeDiscoveryCache()
    _purifier(c, "levoit-office")
    _purifier(c, "levoit-spare", online="offline")
    assert c.candidates(registered={"levoit-office"}, now=101.0) == []


def test_lwt_offline_removes_a_candidate():
    c = D.EsphomeDiscoveryCache()
    _purifier(c)
    c.ingest("levoit-c-office/status", "offline", now=102.0)
    assert c.candidates(registered=set(), now=103.0) == []


def test_non_esphome_topics_ignored():
    c = D.EsphomeDiscoveryCache()
    c.ingest("Bad Name!/status", "online", now=1.0)
    c.ingest("/status", "online", now=1.0)
    assert c.row("Bad Name!") is None and c.candidates(registered=set(), now=2.0) == []


# ── adoption ──────────────────────────────────────────────────────────────────────────────────────

CONTROL = textwrap.dedent("""\
    version: 1
    devices:
      # Air purifier — the first unit.
      purifier_living_room:
        node: server
        area: living_room
        device_type: air_purifier  # inline design note that must survive
        traits:
          switchable: {safe_on: false}
    """)


def _inst(tmp_path):
    (tmp_path / "control.yaml").write_text(CONTROL)
    s = tmp_path / "control_secrets.yaml"
    s.write_text("# secrets\npurifier_living_room: aaaa\n")
    os.chmod(s, 0o600)
    (tmp_path / "levoit-devices.yaml").write_text(
        "# header\nlevoit-office:\n  device_id: purifier_living_room\n  device_type: air_purifier\n")
    (tmp_path / "areas.yaml").write_text("areas:\n  c_office: {}\n  living_room: {}\n")
    return tmp_path


def _adopt(inst, name="levoit-c-office", **body):
    return EI.handle_adopt_purifier(
        name, {"area": "c_office", **body}, control_path=inst / "control.yaml",
        secrets_path=inst / "control_secrets.yaml", levoit_path=inst / "levoit-devices.yaml",
        areas_path=inst / "areas.yaml")


def test_adopt_writes_all_three_and_keeps_comments(tmp_path):
    inst = _inst(tmp_path)
    code, p = _adopt(inst)
    assert code == 201 and p["device_id"] == "purifier_c_office"
    ctl = yaml.safe_load((inst / "control.yaml").read_text())["devices"]
    assert ctl["purifier_c_office"] == {"node": "server", "area": "c_office", "device_type": "air_purifier",
                                        "traits": EI.PURIFIER_TRAITS}
    assert "purifier_living_room" in ctl
    assert "inline design note that must survive" in (inst / "control.yaml").read_text()
    lev = yaml.safe_load((inst / "levoit-devices.yaml").read_text())
    assert lev["levoit-c-office"] == {"device_id": "purifier_c_office", "device_type": "air_purifier"}
    sec = yaml.safe_load((inst / "control_secrets.yaml").read_text())
    assert len(sec["purifier_c_office"]) == 64 and sec["purifier_living_room"] == "aaaa"


def test_secret_never_returned_and_file_stays_private(tmp_path):
    inst = _inst(tmp_path)
    code, p = _adopt(inst)
    secret = yaml.safe_load((inst / "control_secrets.yaml").read_text())["purifier_c_office"]
    assert secret not in repr(p)
    assert os.stat(inst / "control_secrets.yaml").st_mode & 0o777 == 0o600


def test_no_automation_is_seeded(tmp_path):
    inst = _inst(tmp_path)
    """CONFORMANCE §B R2/R4: a purifier's own sensor is never an automatic control source."""
    _adopt(inst)
    assert not (inst / "control_policy.yaml").exists()


def test_second_purifier_in_a_room_gets_a_suffix(tmp_path):
    inst = _inst(tmp_path)
    assert _adopt(inst)[1]["device_id"] == "purifier_c_office"
    assert _adopt(inst, name="levoit-c-office-2")[1]["device_id"] == "purifier_c_office_2"


def test_already_registered_is_409_and_writes_nothing(tmp_path):
    inst = _inst(tmp_path)
    before = {p.name: p.read_bytes() for p in inst.iterdir()}
    code, _ = _adopt(inst, name="levoit-office")
    assert code == 409
    assert before == {p.name: p.read_bytes() for p in inst.iterdir()}


def test_non_canonical_area_rejected(tmp_path):
    inst = _inst(tmp_path)
    code, p = EI.handle_adopt_purifier(
        "levoit-x", {"area": "garage"}, control_path=inst / "control.yaml",
        secrets_path=inst / "control_secrets.yaml", levoit_path=inst / "levoit-devices.yaml",
        areas_path=inst / "areas.yaml")
    assert code == 400 and "canonical" in p["reason"]


def test_dry_run_writes_nothing(tmp_path):
    inst = _inst(tmp_path)
    before = {p.name: p.read_bytes() for p in inst.iterdir()}
    code, p = _adopt(inst, dry_run=True)
    assert code == 200 and p["status"] == "preview" and p["device_id"] == "purifier_c_office"
    assert before == {p.name: p.read_bytes() for p in inst.iterdir()}


def test_layout_that_wont_append_cleanly_rolls_everything_back(tmp_path):
    """control.yaml whose `devices:` is NOT the last key: an appended entry would land under the wrong key.
    The parse-back check must catch it and restore the secret written in step 1 too."""
    inst = _inst(tmp_path)
    (inst / "control.yaml").write_text("devices:\n  purifier_living_room: {node: server, area: living_room}\n"
                                       "version: 1\n")
    before = {p.name: p.read_bytes() for p in inst.iterdir()}
    code, p = _adopt(inst)
    assert code == 500 and p["rolled_back"] == ["control_secrets.yaml"]
    assert before == {p.name: p.read_bytes() for p in inst.iterdir()}


# ── bridge ────────────────────────────────────────────────────────────────────────────────────────

def test_bridge_subscribes_by_topic_shape_not_by_name():
    subs = [t for t, _ in LB.bridge_subscriptions()]
    assert "+/status" in subs and "+/sensor/pm_2_5/state" in subs and "+/fan/fan/state" in subs
    assert not any(t.endswith("/#") for t in subs)      # no per-name catch-alls


if __name__ == "__main__":
    from tests._harness import run_module
    run_module(globals())
