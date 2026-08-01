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
