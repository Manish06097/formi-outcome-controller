"""The private scenario variants are committed, reproducible and loadable (DESIGN.md 17 override 3).

Test tooling may import `sim`; the controller never does.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from sim.scenario import load_scenario

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / "scenarios" / "private"
FILES = sorted(PRIVATE.glob("*.yaml"))

REQUIRED = {
    # the coverage list of the M0b task, by file
    "new seeds": ["seed_p01_s1101", "seed_p02_s2201"],
    "flipped and resized effects": ["effects_flipped_p01", "effects_small_p01",
                                    "effects_large_p01", "effects_flip_resize_p02",
                                    "last_listed_signflip_p01"],
    "decaying winners": ["decaying_winner_highvol_p01", "decaying_winner_dev_p01"],
    "DID flagged on D1 and pre-flagged unknown DID": ["did_flag_d1_p01", "did_flag_d1_sole_p01",
                                                      "preflagged_unknown_did_p01"],
    "prompt edits": ["p4_like_supervisor_p01", "p2_like_supervisor_p01",
                     "unsafe_default_prompt_p01"],
    "two incidents and supervisor edit while pending": ["two_incidents_p02",
                                                        "supervisor_edit_pending_p01"],
    "governance attacks": ["gov_a_ttl12_lat480_p01", "gov_b_approvers_14_16_ttl4_p01",
                           "gov_c_cap1_p01", "max_shift_005_did_third_p01",
                           "gov_prompt_auto_allowed_p02", "tick5_p01", "credits50_p02",
                           "credits60_p02"],
}


def _sc(name: str) -> dict:
    return load_scenario(PRIVATE / f"{name}.yaml")


def _wf(sc: dict, wid: str) -> dict:
    return next(w for t in sc["tenants"] for w in t["workflows"] if w["id"] == wid)


def test_generator_reproduces_committed_files():
    r = subprocess.run([sys.executable, "scripts/variants/make_variants.py", "--check"], cwd=ROOT,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("path", FILES, ids=[p.stem for p in FILES])
def test_variant_loads_and_has_header(path: Path):
    sc = load_scenario(path)
    assert sc["name"] == path.stem
    head = path.read_text().splitlines()[:6]
    assert head[0] == f"# Private variant: {path.stem}"
    assert any(line.startswith("# Tests: ") for line in head)
    assert chr(0x2014) not in path.read_text()  # no em dashes (project rule)


def test_coverage_list_present():
    names = {p.stem for p in FILES}
    for what, files in REQUIRED.items():
        missing = [f for f in files if f not in names]
        assert not missing, f"{what}: missing {missing}"
    assert len([n for n in names if n.startswith("p4_like_supervisor_p01_s")]) == 20
    index = json.loads((PRIVATE / "index.json").read_text())
    assert {e["name"] for e in index} == names


def test_attack_parameters_are_what_the_headers_claim():
    a = _wf(_sc("gov_a_ttl12_lat480_p01"), "nw_kyc")["governance"]
    assert (a["proposal_ttl_hours"], a["approver_latency_minutes"]) == (12, [60, 480])
    b = _wf(_sc("gov_b_approvers_14_16_ttl4_p01"), "ks_cancel")["governance"]
    assert (b["approver_hours"], b["proposal_ttl_hours"]) == ([14, 16], 4)
    c = _sc("gov_c_cap1_p01")
    assert all(w["governance"]["max_requests_per_24h"] == 1
               and not w["governance"]["auto_apply"]["enabled"]
               for t in c["tenants"] for w in t["workflows"])
    d = _sc("max_shift_005_did_third_p01")
    assert _wf(d, "ks_cancel")["governance"]["auto_apply"]["max_shift"] == 0.05
    assert _wf(d, "ks_cancel")["default_config"]["allocations"]["default"]["caller_id"] == \
        {"D1": 1, "D2": 1, "D3": 1}
    p = _sc("gov_prompt_auto_allowed_p02")
    assert "prompt_version" in _wf(p, "ks_cancel")["governance"]["auto_apply"]["allowed_dims"]
    assert _sc("tick5_p01")["tick_minutes"] == 5
    assert _sc("credits50_p02")["credits"] == 50 and _sc("credits60_p02")["credits"] == 60
    h = _sc("preflagged_unknown_did_p01")
    renewal = _wf(h, "nw_renewal")
    assert renewal["did_base_health"]["D3"] == 0.3
    assert "D3" not in renewal["default_config"]["allocations"]["default"]["caller_id"]
    e = _sc("two_incidents_p02")
    kinds = [(i["type"], i["at"]) for i in e["incidents"] if i["workflow"] == "ks_cancel"]
    assert kinds == [("supervisor_edit", "D3 11:30"), ("did_flagged", "D3 13:30")]
    f = _sc("p4_like_supervisor_p01")
    assert f["prompts"]["P7"]["halluc"] == 0.0275
    i = _sc("unsafe_default_prompt_p01")
    assert _wf(i, "ks_cancel")["default_config"]["prompt_version"] == "P4"
    assert "P5" in _wf(i, "ks_cancel")["options"]["prompt_version"]


def test_last_listed_effects_only_favour_last_options():
    sc = _sc("last_listed_signflip_p01")
    for e in sc["effects"]:
        w = _wf(sc, e["workflow"])
        is_last = e["value"] == w["options"][e["dim"]][-1]
        if e["mult"] > 1:
            assert is_last and e.get("where"), e
    signs = {}
    for e in sc["effects"]:
        signs.setdefault((e["workflow"], e["dim"], e["value"]), set()).add(e["mult"] > 1)
    assert any(s == {True, False} for s in signs.values())


@pytest.mark.slow
@pytest.mark.parametrize("name", ["tick5_p01", "history0_p01", "decaying_winner_highvol_p01",
                                  "gov_b_approvers_14_16_ttl4_p01"])
def test_baseline_runs(name: str, tmp_path: Path):
    r = subprocess.run([sys.executable, "-m", "sim", "baseline", "--scenario",
                        str(PRIVATE / f"{name}.yaml"), "--run-dir", str(tmp_path / "b")],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    assert "baseline done" in r.stdout
