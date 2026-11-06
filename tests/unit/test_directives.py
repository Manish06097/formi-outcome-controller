"""Directive scope resolution (DESIGN.md 6.5). The scenario files are read here only to list
real workflow identities; controller code never reads them."""
from __future__ import annotations

from pathlib import Path

import pytest

from controller.world.directives import DirectiveError, load_directives
from sim.scenario import load_scenario


@pytest.fixture(scope="module")
def layer():
    return load_directives()


def _workflows(name: str):
    sc = load_scenario(f"scenarios/{name}.yaml")
    return [(w["tenant_id"], w["id"], w.get("use_case", "")) for t in sc["tenants"]
            for w in t["workflows"]]


def test_northwind_is_tenant_wide(layer):
    for tenant, wid, uc in _workflows("public_03_load"):
        if tenant != "northwind_life":
            continue
        d = layer.resolve(tenant, wid, uc)
        assert d.customer_facing_auto_apply == "none"        # incl. nort_cancel_2 (F15)
        assert d.cohort_harm_line == 0.01 and d.weekly_digest
        assert d.max_requests_per_day is None and d.not_before_min is None
        assert d.scopes == ("*", "northwind_life")


def test_kestrel_asks_apply_to_the_cancellation_line_only(layer):
    seen = 0
    for tenant, wid, uc in _workflows("public_03_load") + _workflows("public_01_dev"):
        if tenant != "kestrel_finance":
            continue
        d = layer.resolve(tenant, wid, uc)
        if "cancel" in uc.lower():
            seen += 1
            assert d.max_requests_per_day == 2 and d.not_before_min == 11 * 60
            assert d.notice_minutes == 60 and d.notice_scope == "non_incident"
            assert d.notice_inside_approver_hours
            assert d.customer_facing_auto_apply == "ramps_and_risk_reducing"
            assert d.avoid_prompt_terms == ("verbatim", "word for word")
            assert d.cohort_harm_line == 0.02
            assert d.sources["max_requests_per_day"].endswith("memo 04")
        else:                                               # renewal, KYC: the * default
            assert d.max_requests_per_day is None and d.not_before_min is None
            assert d.customer_facing_auto_apply == "risk_reducing_only"
    assert seen == 2


def test_unknown_tenant_gets_the_default(layer):
    d = layer.resolve("tidewater_health", "tide_cancel_2", "consumer-loan cancellation")
    assert d.customer_facing_auto_apply == "risk_reducing_only"
    assert d.notice_minutes == 60 and d.notice_scope == "customer_facing"
    assert d.cohort_harm_line == 0.02 and d.pre_open_submits == "only_if_no_safe_in_hours"
    assert d.preferred_slot_offset_max_min == 60 and d.preferred_slot_offset_window_frac == 0.25
    assert d.scopes == ("*",)


def test_rows_have_unique_keys_for_the_directive_table(layer):
    rows = layer.rows()
    keys = [(r[0], r[1]) for r in rows]
    assert len(keys) == len(set(keys))
    assert ("kestrel_finance/use_case~cancel", "not_before", "11:00", "memo 04") in rows


def test_more_specific_scope_wins(tmp_path: Path):
    p = tmp_path / "d.yaml"
    p.write_text("""
version: 1
defaults:
  source: d
  set: {customer_facing_auto_apply: risk_reducing_only, notice_minutes: 60,
        notice_scope: customer_facing, notice_inside_approver_hours: false,
        max_requests_per_day: null, not_before: null, avoid_prompt_terms: [],
        weekly_digest: false, pre_open_submits: only_if_no_safe_in_hours}
directives:
  - {tenant: acme, selector: {workflow_id: acme_kyc}, source: w, set: {max_requests_per_day: 1}}
  - {tenant: acme, selector: {}, source: t, set: {max_requests_per_day: 3, notice_minutes: 30}}
  - {tenant: "*", selector: {use_case_contains: KYC}, source: u, set: {max_requests_per_day: 2}}
""")
    layer = load_directives(p)
    d = layer.resolve("acme", "acme_kyc", "kyc reminders")
    assert d.max_requests_per_day == 1 and d.notice_minutes == 30
    d2 = layer.resolve("acme", "acme_other", "kyc reminders")
    assert d2.max_requests_per_day == 2          # use-case rule beats tenant-wide
    d3 = layer.resolve("zeta", "z", "KYC")
    assert d3.max_requests_per_day == 2 and d3.notice_minutes == 60


@pytest.mark.parametrize("bad", [
    "version: 2\ndefaults: {source: d, set: {}}\n",
    "version: 1\ndefaults: {source: d, set: {notice_minutes: 60}}\n",
    "version: 1\nextra: 1\ndefaults: {source: d, set: {}}\n",
])
def test_malformed_files_are_refused(tmp_path: Path, bad: str):
    p = tmp_path / "d.yaml"
    p.write_text(bad)
    with pytest.raises(DirectiveError):
        load_directives(p)


def test_bad_values_are_refused(tmp_path: Path):
    base = load_directives()
    text = Path("controller/directives.yaml").read_text().replace('not_before: "11:00"',
                                                                  'not_before: "25:00"')
    p = tmp_path / "d.yaml"
    p.write_text(text)
    with pytest.raises(DirectiveError):
        load_directives(p)
    assert base.resolve("kestrel_finance", "ks_cancel", "cancel").not_before_min == 660
