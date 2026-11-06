"""Decision replay (DESIGN.md 9.6) on the fixture store, with a toy gate as the registered
recomputer, standing in for the learning tracks' gates until they land."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from controller.audit import replay
from controller.settings import load_settings
from tests.audit import fixture_world as fw

pytestmark = pytest.mark.pg
ROOT = Path(__file__).resolve().parents[2]


def _toy(ctx: replay.ReplayContext) -> dict:
    mean, cov = ctx.fit("ew", refit=fw.toy_fit)
    g = fw.toy_gate(mean, cov, ctx.seed, int(ctx.inputs_json["n_draws"]))
    return {"outcome": g, "harm_report": {"de_mean": g["de_mean"],
                                          "p_de_below_1pct": g["p_de_below_1pct"]}}


@pytest.fixture(autouse=True)
def toy_recomputer():
    replay.register_recomputer("test.toy", "toy_gate")(_toy)
    yield
    replay.unregister_recomputer("test.toy", "toy_gate")


def test_codec_round_trip_and_stable_hash():
    arrays = {"b": np.arange(6).reshape(2, 3), "a": np.array([0.5, 1e-12])}
    back = replay.decode_inputs(replay.encode_inputs(arrays))
    assert set(back) == {"a", "b"} and np.array_equal(back["b"], arrays["b"])
    assert replay.hash_inputs(back) == replay.hash_inputs(arrays)
    assert replay.hash_inputs({"a": arrays["a"].astype(np.float32), "b": arrays["b"]}) != \
        replay.hash_inputs(arrays)
    m = np.array([[1.0, 2.0]])
    assert np.array_equal(replay.decode_array(replay.encode_array(m)), m)


def test_compare_rounds_and_recurses_only_into_recomputed_keys():
    stored = {"a": 0.1 + 0.2, "b": [1, 2], "c": {"x": 1, "y": "z"}, "extra": 5}
    assert replay.compare(stored, {"a": 0.3, "b": [1, 2], "c": {"x": 1}}) == []
    assert replay.compare(stored, {"a": 0.25}) == ["a: stored 0.30000000000000004, "
                                                   "recomputed 0.25"]
    assert replay.compare({"a": 1.0}, {"a": 1.0 + 1e-7}) != []  # beyond 1e-9
    assert replay.compare({"a": 1.0}, {"a": 1.0 + 1e-10}) == []
    assert replay.compare(stored, {"b": [1]})[0] == "b: stored 2 items, recomputed 1"
    assert replay.compare(stored, {"missing": 1})[0].startswith("missing: not in the stored")
    assert replay.compare({"f": True}, {"f": False}) == ["f: stored True, recomputed False"]


def test_replay_reproduces_the_stored_decision(conn):
    rep = replay.replay_decision(1, conn=conn, settings=load_settings())
    assert rep.equal, rep.diffs
    assert rep.settings_hash_match and rep.code_version_match
    assert rep.recomputed["outcome"]["p_de_below_1pct"] == rep.stored["outcome"]["p_de_below_1pct"]
    rep = replay.replay_decision(1, refit=False, conn=conn, settings=load_settings())
    assert rep.equal, rep.diffs
    assert "RESULT: equal" in replay.render_report(rep)


def test_replay_detects_a_changed_outcome(conn):
    conn.execute("UPDATE decision SET outcome = jsonb_set(outcome, '{p_de_below_1pct}', '0.5') "
                 "WHERE decision_id = 1")
    rep = replay.replay_decision(1, conn=conn, settings=load_settings())
    assert not rep.equal
    assert any(d.startswith("outcome.p_de_below_1pct: stored 0.5") for d in rep.diffs)


def test_replay_detects_tampered_inputs(conn):
    bad = {"engaged": np.array([400, 90]), "connected": np.array([800, 95])}
    conn.execute("UPDATE posterior_snapshot SET inputs = %s WHERE snapshot_id = 1",
                 (replay.encode_inputs(bad),))
    rep = replay.replay_decision(1, conn=conn, settings=load_settings())
    assert not rep.equal
    assert any("do not match inputs_hash" in d for d in rep.diffs)
    assert any("refitted mean differs" in d for d in rep.diffs)
    # without refit the stored posterior is reused, but the hash check still fails
    rep = replay.replay_decision(1, refit=False, conn=conn, settings=load_settings())
    assert any("inputs_hash" in d for d in rep.diffs)


def test_replay_says_when_settings_or_code_differ(conn):
    other = load_settings({"CTL_T_ALPHA_H": "0.05", "CTL_CODE_VERSION": "abc123"})
    rep = replay.replay_decision(1, conn=conn, settings=other)
    assert rep.equal  # the toy gate does not read alpha_h, so the values still match
    assert not rep.settings_hash_match and not rep.code_version_match
    text = replay.render_report(rep)
    assert text.splitlines()[0].startswith("WARNING: decision 1 was made with settings")


def test_unregistered_rule_is_not_equal(conn):
    rep = replay.replay_decision(2, conn=conn, settings=load_settings())
    assert not rep.equal and "no recomputer registered for learning.did/did_probe" in rep.diffs[0]


def test_twenty_sampled_decisions_replay_exactly(conn):
    """The M7 acceptance shape: replay-decision reproduces 20 sampled decisions exactly."""
    s = load_settings()
    rng = np.random.default_rng(7)
    for i in range(25):
        inputs = {"engaged": rng.integers(100, 400, 2), "connected": np.array([800, 700])}
        mean, cov = fw.toy_fit(inputs)
        seed = int(rng.integers(1, 2 ** 62))
        g = fw.toy_gate(mean, cov, seed, 2000)
        did = conn.execute(
            "INSERT INTO decision (sim_minute, wf, module, kind, rule_id, inputs, outcome, "
            "harm_report, seed, settings_hash, code_version) VALUES (%s, 1, 'test.toy', 'explore', "
            "'toy_gate', %s, %s, %s, %s, %s, 'dev') RETURNING decision_id",
            (100 + i, json.dumps({"n_draws": 2000}), json.dumps(g),
             json.dumps({"de_mean": g["de_mean"], "p_de_below_1pct": g["p_de_below_1pct"]}),
             seed, s.settings_hash())).fetchone()[0]
        sid = conn.execute(
            "INSERT INTO posterior_snapshot (wf, decision_id, kind, inputs, inputs_hash, mean, "
            "cov, seed) VALUES (1, %s, 'ew', %s, %s, %s, %s, %s) RETURNING snapshot_id",
            (did, replay.encode_inputs(inputs), replay.hash_inputs(inputs),
             replay.encode_array(mean), replay.encode_array(cov), seed)).fetchone()[0]
        conn.execute("UPDATE decision SET snapshot_ids = ARRAY[%s]::bigint[] "
                     "WHERE decision_id = %s", (sid, did))
    ids = replay.sample_decisions(conn, n=20, seed=3)
    assert len(ids) == 20 and ids == replay.sample_decisions(conn, n=20, seed=3)
    assert 2 not in ids  # no recomputer for learning.did
    reps = [replay.replay_decision(i, conn=conn, settings=s) for i in ids]
    assert all(r.equal for r in reps), [r.diffs for r in reps if not r.equal]


def test_cli_replay_exit_codes(world):
    r = subprocess.run([sys.executable, "-m", "controller.audit", "--dsn", world["dsn"],
                        "--schema", world["schema"], "replay-decision", "2"], cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "no recomputer registered" in r.stdout
    r = subprocess.run([sys.executable, "-m", "controller.audit", "--dsn", world["dsn"],
                        "--schema", world["schema"], "replay-decision", "999"], cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "no decision 999" in r.stderr
