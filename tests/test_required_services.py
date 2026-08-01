"""Tests for the required-services resolver (tools/required_services.py).

Focus: the co-resident second-stack support added for board task `ag-unit-set-gaps`. The air-gap
standby's `ha-ag-*` units were hand-curated and therefore invisible to the supervisor — which is how it
ended up with a full ingest path and no compactor, unnoticed until it caused a four-day failover outage.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tools import required_services as RS  # noqa: E402


# ── co-resident second stack: unit_prefix + exclude (board ag-unit-set-gaps) ──────────
# .210 runs the household `ha-*` set AND the air-gap standby's `ha-ag-*` set. The second set used to be
# hand-curated and therefore un-auditable — which is how it acquired a full ingest path and no compactor.

def _mini_manifest():
    return {"roles": {"core": {"units": [
        {"unit": "ha-writer.service", "must": "active"},
        {"unit": "ha-scanner.service", "must": "active"},
        {"unit": "ha-rollup.timer", "must": "active"},
    ]}}}


def test_unit_prefix_rewrites_to_the_second_stack():
    got = RS.resolve(_mini_manifest(), ["core"], {"unit_prefix": "ha-ag-", "exclude": set()})
    assert set(got) == {"ha-ag-writer.service", "ha-ag-scanner.service", "ha-ag-rollup.timer"}


def test_no_prefix_leaves_names_untouched():
    got = RS.resolve(_mini_manifest(), ["core"], None)
    assert set(got) == {"ha-writer.service", "ha-scanner.service", "ha-rollup.timer"}


def test_exclude_matches_the_manifest_name_not_the_prefixed_one():
    """So an exclude list reads identically on every box, whatever that box's prefix is."""
    got = RS.resolve(_mini_manifest(), ["core"],
                     {"unit_prefix": "ha-ag-", "exclude": {"ha-scanner.service"}})
    assert "ha-ag-scanner.service" not in got
    assert "ha-ag-writer.service" in got


def test_excluding_by_prefixed_name_does_not_work():
    """Pins the contract above — the prefixed form must NOT silently exclude, or a typo'd exclude entry
    would quietly drop a required unit instead of erroring loudly as a GAP."""
    got = RS.resolve(_mini_manifest(), ["core"],
                     {"unit_prefix": "ha-ag-", "exclude": {"ha-ag-scanner.service"}})
    assert "ha-ag-scanner.service" in got


def test_prefix_only_applies_to_ha_units():
    m = {"roles": {"core": {"units": [{"unit": "mosquitto.service", "must": "active"}]}}}
    got = RS.resolve(m, ["core"], {"unit_prefix": "ha-ag-", "exclude": set()})
    assert set(got) == {"mosquitto.service"}


def test_host_options_defaults_are_inert(tmp_path):
    p = tmp_path / "host-roles.yaml"
    p.write_text("roles: [core]\n")
    o = RS.host_options(p)
    assert o["unit_prefix"] == "" and o["exclude"] == set()


def test_host_options_reads_prefix_and_exclude(tmp_path):
    p = tmp_path / "host-roles.yaml"
    p.write_text("roles: [core]\nunit_prefix: ha-ag-\nexclude: [ha-scanner.service]\n")
    o = RS.host_options(p)
    assert o["unit_prefix"] == "ha-ag-" and o["exclude"] == {"ha-scanner.service"}


# ── batched unit_states (board os-idle-churn) ─────────────────────────────────────────
# Was two `systemctl` subprocesses PER UNIT: 82 processes per supervisor run on a 41-unit host, on a box
# that runs the check for two stacks. The parsing must keep the old vocabulary exactly, because
# `not-found` is the dangerous "never provisioned" case the whole supervisor exists to catch.

def test_unit_states_parses_a_show_block(monkeypatch):
    import subprocess
    out = ("Id=ha-api.service\nLoadState=loaded\nUnitFileState=enabled\nActiveState=active\n"
           "\n"
           "Id=ha-gone.service\nLoadState=not-found\nUnitFileState=\nActiveState=inactive\n")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": out, "stderr": ""})())
    s = RS.unit_states(["ha-api.service", "ha-gone.service"])
    assert s["ha-api.service"] == ("enabled", "active")
    assert s["ha-gone.service"][0] == "not-found"      # the never-provisioned signal must survive


def test_unit_states_is_one_subprocess_call(monkeypatch):
    """The whole point: N units, ONE spawn."""
    import subprocess
    calls = []
    def fake(cmd, **k):
        calls.append(cmd)
        return type("R", (), {"stdout": "".join(
            f"Id={u}\nLoadState=loaded\nUnitFileState=enabled\nActiveState=active\n\n"
            for u in cmd[cmd.index("--") + 1:]), "stderr": ""})()
    monkeypatch.setattr(subprocess, "run", fake)
    RS.unit_states([f"ha-u{i}.service" for i in range(20)])
    assert len(calls) == 1


def test_unit_states_empty_input_spawns_nothing(monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not spawn")))
    assert RS.unit_states([]) == {}


def test_unit_states_never_silently_drops_a_unit(monkeypatch):
    """If `show` omits a unit, it must fall back per-unit — an unaccounted unit must never read as
    healthy, which would hide exactly the gap we are looking for."""
    import subprocess
    seq = []
    def fake(cmd, **k):
        seq.append(cmd)
        if "show" in cmd:
            return type("R", (), {"stdout": "Id=ha-a.service\nLoadState=loaded\n"
                                            "UnitFileState=enabled\nActiveState=active\n", "stderr": ""})()
        return type("R", (), {"stdout": "not-found\n", "stderr": ""})()
    monkeypatch.setattr(subprocess, "run", fake)
    s = RS.unit_states(["ha-a.service", "ha-missing.service"])
    assert "ha-missing.service" in s
    assert len(seq) > 1                                # fell back rather than dropping it
