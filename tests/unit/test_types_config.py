"""Config normalisation and changed-dims equality, checked against the simulator's own code.

Tests may import `sim` (controller code may not). Every property here is differential: the
controller's `Config` must agree with `sim/world.py` bit for bit, because the policy replica
(governance/policy.py) is built on it and a disagreement means a 403 or a spurious change.
"""
from __future__ import annotations

import copy
import pickle
import random

import pytest

from controller.types import (AGENT_DIMS, CohortDef, Config, ConfigInvalid, FrozenDict,
                              WorkflowSpec, _preimage, _sim_normalise)
from sim import world as simworld
from sim.scenario import load_scenario

SCENARIO = "scenarios/public_01_dev.yaml"


@pytest.fixture(scope="module")
def spec_raw() -> dict:
    sc = load_scenario(SCENARIO)
    wf = sc["tenants"][0]["workflows"][0]          # nw_renewal: 3 welcomes, 4 voices, 4 DIDs
    return wf


@pytest.fixture(scope="module")
def spec(spec_raw) -> WorkflowSpec:
    s = spec_raw
    public = {"workflow_id": s["id"], "tenant_id": s["tenant_id"], "use_case": s["use_case"],
              "languages": list(s["language_mix"]), "dialling_window": s["dialling_window"],
              "dials_per_hour": s["dials_per_hour"], "cadence": s["cadence"],
              "min_gap_minutes": s["min_gap_minutes"], "daily_batch": s["daily_batch"],
              "mid_flight": s["mid_flight"], "objective": s["objective"],
              "options": s["options"], "prompt_catalog": {}, "governance": s["governance"],
              "current_version": 1}
    return WorkflowSpec.from_api(public)


def _random_weights(rng: random.Random, opts: list[str]) -> dict[str, float]:
    k = rng.randint(1, len(opts))
    chosen = rng.sample(opts, k)
    w = {o: round(rng.random(), rng.choice([1, 2, 3, 6, 17])) for o in chosen}
    if sum(w.values()) <= 0:
        w[chosen[0]] = 1
    if rng.random() < 0.2:
        w[chosen[0]] = 0          # a zero weight: the simulator drops it after normalising
        if sum(w.values()) <= 0:
            w[chosen[-1]] = 0.5
    return w


def _random_body(rng: random.Random, spec_raw: dict) -> dict:
    opts = spec_raw["options"]
    cohorts = []
    pool = [("age_band", ["18-30", "31-45", "46-60", "60+"]), ("lang", ["hi", "en", "ta"]),
            ("city_tier", ["1", "2", "3"])]
    for i in range(rng.randint(0, 3)):
        f, vals = rng.choice(pool)
        cohorts.append({"id": f"c{i}", "where": {f: rng.sample(vals, rng.randint(1, len(vals)))}})
    allocs = {"default": {d: _random_weights(rng, opts[d]) for d in AGENT_DIMS}}
    for c in cohorts:
        if rng.random() < 0.6:
            allocs[c["id"]] = {d: _random_weights(rng, opts[d])
                               for d in rng.sample(list(AGENT_DIMS), rng.randint(1, 3))}
    lo, hi = spec_raw["dialling_window"]
    hw = {}
    if rng.random() < 0.5:
        hw["default"] = {str(h): rng.choice([0, 0.25, 1, 2.5]) for h in range(lo, hi)
                         if rng.random() < 0.8}
    lph = {f"nw_renewal:p{rng.randint(0, 999):07d}": rng.randint(lo, hi - 1)
           for _ in range(rng.randint(0, 3))}
    return {"cohorts": cohorts, "allocations": allocs,
            "prompt_version": rng.choice(opts["prompt_version"]),
            "schedule": {"hour_weights": hw, "lead_preferred_hour": lph}}


def test_from_body_matches_simulator_normalisation_bit_for_bit(spec_raw):
    rng = random.Random(7)
    for _ in range(2000):
        body = _random_body(rng, spec_raw)
        ours = Config.from_body(body).stored()
        theirs = simworld.normalise_config(spec_raw, copy.deepcopy(body))
        assert ours == theirs


def test_body_round_trips_through_the_simulator(spec_raw):
    """Sending body() makes the simulator store exactly this config; as_stored() always predicts
    what it will store, even in the rare case no exact preimage was found."""
    rng = random.Random(11)
    exact = 0
    n = 2000
    for _ in range(n):
        body = _random_body(rng, spec_raw)
        stored_by_sim = simworld.normalise_config(spec_raw, copy.deepcopy(body))
        # a config fetched back from the API, then re-sent unchanged
        cfg = Config.from_stored(stored_by_sim)
        resent = simworld.normalise_config(spec_raw, cfg.body())
        assert Config.from_stored(resent) == cfg.as_stored()
        if cfg.wire_exact():
            exact += 1
            assert resent == stored_by_sim
            assert simworld.changed_dims(stored_by_sim, resent) == []
    assert exact / n >= 0.99


def test_raw_weights_sent_back_exactly(spec_raw):
    """A config built from raw weights keeps them for the wire, so the round trip is exact."""
    body = _random_body(random.Random(3), spec_raw)
    body["allocations"]["default"]["caller_id"] = {"D1": 0.1, "D2": 0.25, "D3": 0.35, "D4": 0.3}
    cfg = Config.from_body(body)
    assert cfg.wire_exact()
    assert simworld.normalise_config(spec_raw, cfg.body()) == cfg.stored()


def test_renormalising_a_stored_distribution_can_change_its_bits():
    """The trap `_preimage` exists for: a stored distribution re-sent verbatim can drift."""
    rng = random.Random(1)
    drifted = 0
    for _ in range(500):
        w = {f"o{i}": round(rng.random(), 17) for i in range(rng.randint(2, 5))}
        v = _sim_normalise(w)
        if _sim_normalise(v) != v:
            drifted += 1
            pre = _preimage(tuple(v.items()))
            if pre is not None:
                assert _sim_normalise(dict(pre)) == v
    assert drifted > 0


def test_normalised_weights_and_zero_drop_are_equal():
    a = Config.from_body({"allocations": {"default": {"welcome_message": {"W1": 2, "W2": 2},
                                                      "voice": {"V1": 1, "V2": 0},
                                                      "caller_id": {"D1": 1}}},
                          "prompt_version": "P1"})
    b = Config.from_body({"allocations": {"default": {"welcome_message": {"W1": .5, "W2": .5},
                                                      "voice": {"V1": 1.0},
                                                      "caller_id": {"D1": 3}}},
                          "prompt_version": "P1"})
    assert a == b and a.hash() == b.hash() and hash(a) == hash(b)
    assert a.dist("default", "voice") == {"V1": 1.0}
    assert "V2" not in a.body()["allocations"]["default"]["voice"]


def test_changed_dims_matches_simulator_on_random_pairs(spec_raw):
    rng = random.Random(5)
    for _ in range(3000):
        b1 = _random_body(rng, spec_raw)
        b2 = _random_body(rng, spec_raw) if rng.random() < 0.5 else _mutate(rng, b1, spec_raw)
        s1 = simworld.normalise_config(spec_raw, copy.deepcopy(b1))
        s2 = simworld.normalise_config(spec_raw, copy.deepcopy(b2))
        c1, c2 = Config.from_stored(s1), Config.from_stored(s2)
        assert c1.changed_dims(c2) == simworld.changed_dims(s1, s2)
        assert c1.equivalent(c2) == (simworld.changed_dims(s1, s2) == [])


def _mutate(rng: random.Random, body: dict, spec_raw: dict) -> dict:
    b = copy.deepcopy(body)
    choice = rng.randrange(7)
    if choice == 0 and len(b["cohorts"]) >= 2:
        b["cohorts"].reverse()                                   # order matters
    elif choice == 1 and b["cohorts"]:
        for vals in b["cohorts"][0]["where"].values():
            vals.reverse()                                       # value order matters
    elif choice == 2 and b["cohorts"]:
        cid = b["cohorts"][0]["id"]                              # override equal to default
        b["allocations"][cid] = {"voice": dict(b["allocations"]["default"]["voice"])}
    elif choice == 3:
        b["schedule"]["lead_preferred_hour"]["nw_renewal:p0000001"] = spec_raw["dialling_window"][0]
    elif choice == 4:
        b["schedule"]["hour_weights"].setdefault("default", {})  # empty table differs from none
    elif choice == 5:
        d = b["allocations"]["default"]["caller_id"]
        k = next(iter(d))
        d[k] = d[k] * 2 + 1                                      # a real change
    else:
        for dims in b["allocations"].values():                   # scaling: no change
            for ws in dims.values():
                for o in ws:
                    ws[o] = ws[o] * 4
    return b


def test_override_equal_to_default_is_not_a_change_but_is_structurally_different():
    base = {"cohorts": [{"id": "young", "where": {"age_band": ["18-30"]}}],
            "allocations": {"default": {"welcome_message": {"W1": 1}, "voice": {"V1": 1},
                                        "caller_id": {"D1": 1}}},
            "prompt_version": "P1"}
    with_override = copy.deepcopy(base)
    with_override["allocations"]["young"] = {"voice": {"V1": 1}}
    a, b = Config.from_body(base), Config.from_body(with_override)
    assert a != b
    assert a.changed_dims(b) == []
    assert b.has_override("young", "voice") and not a.has_override("young", "voice")
    assert a.dist("young", "voice") == b.dist("young", "voice") == {"V1": 1.0}


def test_cohort_where_value_order_and_cohort_order_count():
    c1 = CohortDef.make("a", {"age_band": ["60+", "18-30"], "lang": ["hi"]})
    c2 = CohortDef.make("a", {"lang": ["hi"], "age_band": ["60+", "18-30"]})
    c3 = CohortDef.make("a", {"age_band": ["18-30", "60+"], "lang": ["hi"]})
    assert c1 == c2          # feature (key) order is irrelevant, as in dict equality
    assert c1 != c3          # value order is relevant, as in list equality
    assert c1.matches({"age_band": "60+", "lang": "hi"})
    assert not c1.matches({"age_band": "60+", "lang": "en"})


def test_dist_falls_back_to_default_and_prompt_dist():
    cfg = Config.from_body({"cohorts": [{"id": "old", "where": {"age_band": ["60+"]}}],
                            "allocations": {"default": {"welcome_message": {"W1": 1},
                                                        "voice": {"V1": 1},
                                                        "caller_id": {"D1": 1, "D2": 1}},
                                            "old": {"voice": {"V3": 1}}},
                            "prompt_version": "P3"})
    assert cfg.dist("old", "voice") == {"V3": 1.0}
    assert cfg.dist("old", "caller_id") == {"D1": 0.5, "D2": 0.5}
    assert cfg.dist("nobody", "welcome_message") == {"W1": 1.0}
    assert cfg.dist("old", "prompt_version") == {"P3": 1.0}
    assert cfg.cohort_of({"age_band": "60+", "lang": "hi"}) == "old"
    assert cfg.cohort_of({"age_band": "18-30"}) == "default"
    with pytest.raises(ValueError):
        cfg.dist("old", "schedule")


def test_builders_and_hash_identity():
    cfg = Config.from_body({"allocations": {"default": {"welcome_message": {"W1": 1},
                                                        "voice": {"V1": 1},
                                                        "caller_id": {"D1": 1}}},
                            "prompt_version": "P1"})
    c2 = cfg.with_allocation("default", "caller_id", {"D1": 0.6, "D2": 0.4})
    assert cfg.changed_dims(c2) == ["caller_id"] and c2.wire_exact()
    c3 = c2.with_hour_weights("default", {9: 1, 10: 2}).with_prompt("P3")
    assert cfg.changed_dims(c3) == ["caller_id", "prompt_version", "schedule"]
    assert len({cfg.hash(), c2.hash(), c3.hash()}) == 3
    assert pickle.loads(pickle.dumps(c3)) == c3
    assert Config.from_stored(c3.stored()) == c3


def test_problems_mirror_simulator_422(spec_raw, spec):
    good = {"allocations": {"default": {"welcome_message": {"W1": 1}, "voice": {"V1": 1},
                                        "caller_id": {"D1": 1}}}, "prompt_version": "P1"}
    assert Config.from_body(good).problems(spec) == []
    bad_cases = [
        {**good, "prompt_version": "P9"},
        {**good, "allocations": {**good["allocations"], "ghost": {"voice": {"V1": 1}}}},
        {**good, "allocations": {"default": {"welcome_message": {"W1": 1}, "voice": {"V9": 1},
                                             "caller_id": {"D1": 1}}}},
        {**good, "schedule": {"hour_weights": {"default": {"7": 1}}}},
        {**good, "cohorts": [{"id": "default", "where": {}}]},
    ]
    for body in bad_cases:
        with pytest.raises(simworld.ApiError):
            simworld.normalise_config(spec_raw, copy.deepcopy(body))
        assert Config.from_body(body).problems(spec)


def test_from_body_rejects_what_the_simulator_cannot_normalise():
    with pytest.raises(ConfigInvalid):
        Config.from_body({"allocations": {"default": {"voice": {"V1": 0}}}})
    with pytest.raises(ConfigInvalid):
        Config.from_body({"allocations": {"default": {"voice": {"V1": -1, "V2": 2}}}})


def test_frozen_dict_is_immutable_and_hashable():
    f = FrozenDict({"a": 1})
    with pytest.raises(TypeError):
        f["b"] = 2
    with pytest.raises(TypeError):
        f.update(b=2)
    assert f == {"a": 1} and hash(f) == hash(FrozenDict({"a": 1}))
    assert pickle.loads(pickle.dumps(f)) == f
