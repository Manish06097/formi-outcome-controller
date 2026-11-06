"""Differential test of the local policy replica against a live simulator (DESIGN.md 6.5, ADR 0014).

Starts a real simulator (a copy of public_01 with three different auto-apply policies, port 8712)
and sends at least 2,000 generated auto-apply requests (`PUT /workflows/{id}/config`). For each one
it predicts, before sending, the simulator's exact answer with `governance.policy.sim_verdict`:

- the changed dims (exact list: on 200 from the response; on 403 from the "dims not permitted"
  problem, which on the auto-apply-disabled workflow lists every changed dim),
- the max TV shift (to the 3 decimals the simulator prints) and whether it exceeds max_shift,
- "no change", "not enabled", and the full ordered problem list,
- on 200, that the stored config equals our normalisation bit for bit.

It also checks the one-sided guarantee our controller relies on: whenever our stricter rule (the
replica with a 1e-6 margin) permits, the simulator accepts (no 403 ever).

Every 403 is logged by the simulator as `auto_apply_rejected`; that is expected here.
"""
from __future__ import annotations

import copy
import random
import re
from collections import Counter
from pathlib import Path

import pytest
import yaml

from controller.governance.policy import max_shift, sim_verdict
from controller.simclient import SimClient
from controller.simclient import errors as E
from controller.types import AGENT_DIMS, Config, ConfigInvalid, WorkflowSpec
from sim.scenario import FEATURES          # tests may read the simulator's feature vocabulary
from tests.support.sim_process import REPO, SimProcess

pytestmark = pytest.mark.e2e

PORT = 8712
SCENARIO = "scenarios/public_01_dev.yaml"
N_PAIRS = 2100
SEED = 20261106

# Three policies: exact changed dims (disabled), a tight shift cap on every dim, the shipped cancel.
POLICIES = {
    "nw_kyc": {"enabled": False, "allowed_dims": [], "max_shift": 0},
    "nw_renewal": {"enabled": True, "allowed_dims": ["cohorts", "welcome_message", "voice",
                                                     "caller_id", "prompt_version", "schedule"],
                   "max_shift": 0.05},
    "ks_cancel": {"enabled": True, "allowed_dims": ["schedule", "caller_id", "welcome_message",
                                                    "voice"], "max_shift": 0.2},
}


def _tenants_with_policies() -> list[dict]:
    doc = yaml.safe_load((REPO / SCENARIO).read_text())
    tenants = copy.deepcopy(doc["tenants"])
    for t in tenants:
        for w in t["workflows"]:
            if w["id"] in POLICIES:
                w["governance"]["auto_apply"] = POLICIES[w["id"]]
    return tenants


@pytest.fixture(scope="module")
def sim(tmp_path_factory):
    work = tmp_path_factory.mktemp("policy_diff")
    overrides = {"tenants": _tenants_with_policies(), "history_days": 1}
    with SimProcess(SCENARIO, Path(work), port=PORT, overrides=overrides) as proc:
        client = SimClient(proc.url, timeout_s=60)
        try:
            yield client
        finally:
            client.close()


# ---------------------------------------------------------------- pair generator
class PairGen:
    """Random proposed bodies, valid for the workflow, biased toward the edges of the policy."""

    def __init__(self, rng: random.Random, spec: WorkflowSpec):
        self.rng, self.spec = rng, spec
        self.max_shift = spec.governance.auto_apply.max_shift
        self.n = 0

    # -- weights
    def _weights(self, opts: list[str]) -> dict[str, float]:
        r = self.rng
        k = r.randint(1, len(opts))
        chosen = r.sample(opts, k)
        style = r.random()
        if style < 0.3:
            w = {o: r.randint(0, 5) for o in chosen}
            if not any(w.values()):
                w[chosen[0]] = 1
        elif style < 0.6:
            w = {o: r.random() for o in chosen}
        else:
            w = {o: round(r.random(), 3) + 1e-3 for o in chosen}
        if r.random() < 0.2:
            w[r.choice(opts)] = 0      # an explicit zero weight is dropped by the simulator
        return w

    def _shift(self, dist: dict[str, float], opts: list[str]) -> dict[str, float]:
        """Move exactly delta of mass between two options: TV = delta (up to float noise)."""
        r = self.rng
        src = max(dist, key=dist.get)
        dst = r.choice([o for o in opts if o != src] or [src])
        cap = self.max_shift or 0.05
        delta = r.choice([cap, cap - 1e-7, cap + 1e-7, cap - 2e-6, cap + 1e-3, cap - 1e-3,
                          cap / 2, cap * r.random(), 1e-10, cap + 5e-10])
        delta = max(0.0, min(delta, dist[src]))
        out = dict(dist)
        out[src] = dist[src] - delta
        out[dst] = out.get(dst, 0.0) + delta
        return out

    # -- cohorts
    def _where(self) -> dict[str, list[str]]:
        r = self.rng
        feats = r.sample(list(FEATURES), r.randint(1, 2))
        return {f: r.sample(FEATURES[f], r.randint(1, 2)) for f in feats}

    def body(self, live: Config) -> dict:
        r = self.rng
        b = live.stored() if r.random() < 0.5 else live.body()
        mutations = r.choice([0, 1, 1, 1, 2, 2, 3])
        for _ in range(mutations):
            self._mutate(b, live)
        return b

    def _mutate(self, b: dict, live: Config) -> None:
        r, spec = self.rng, self.spec
        kind = r.choices(
            ["shift", "weights", "scale", "cohort_add", "cohort_remove", "cohort_reorder",
             "cohort_override", "override_equals_default", "prompt", "hours", "hours_same",
             "lph", "remove_override"],
            weights=[30, 12, 8, 6, 3, 2, 10, 4, 4, 8, 3, 3, 4])[0]
        allocs = b["allocations"]
        ids = [c["id"] for c in b["cohorts"]]
        keys = ["default"] + [k for k in ids]
        if kind == "shift":
            k = r.choice([k for k in allocs] or ["default"])
            dim = r.choice(AGENT_DIMS)
            dist = allocs.get(k, {}).get(dim) or dict(live.dist("default", dim))
            allocs.setdefault(k, {})[dim] = self._shift(dict(dist), list(spec.options[dim]))
        elif kind == "weights":
            k = r.choice(keys)
            dim = r.choice(AGENT_DIMS)
            allocs.setdefault(k, {})[dim] = self._weights(list(spec.options[dim]))
        elif kind == "scale":
            k = r.choice(list(allocs))
            for dim, ws in list(allocs[k].items()):
                f = r.choice([2, 3, 7, 0.1, 1 / 3, 10.0])
                allocs[k][dim] = {o: w * f for o, w in ws.items()}
        elif kind == "cohort_add" and len(ids) < 20:
            cid = f"c{self.n}_{r.randint(0, 999)}"
            self.n += 1
            b["cohorts"].insert(r.randint(0, len(ids)), {"id": cid, "where": self._where()})
        elif kind == "cohort_remove" and ids:
            cid = r.choice(ids)
            b["cohorts"] = [c for c in b["cohorts"] if c["id"] != cid]
            allocs.pop(cid, None)
            b["schedule"]["hour_weights"].pop(cid, None)
        elif kind == "cohort_reorder" and len(ids) >= 2:
            r.shuffle(b["cohorts"])
        elif kind == "cohort_override" and ids:
            cid = r.choice(ids)
            dim = r.choice(AGENT_DIMS)
            base = dict(live.dist(cid, dim))
            allocs.setdefault(cid, {})[dim] = (self._shift(base, list(spec.options[dim]))
                                               if r.random() < 0.6
                                               else self._weights(list(spec.options[dim])))
        elif kind == "override_equals_default" and ids:
            cid = r.choice(ids)
            dim = r.choice(AGENT_DIMS)
            allocs.setdefault(cid, {})[dim] = dict(allocs["default"][dim])
        elif kind == "remove_override":
            ks = [k for k in allocs if k != "default" and allocs[k]]
            if ks:
                k = r.choice(ks)
                allocs[k].pop(r.choice(list(allocs[k])))
        elif kind == "prompt":
            b["prompt_version"] = r.choice(list(spec.options["prompt_version"]))
        elif kind in ("hours", "hours_same"):
            lo, hi = spec.dialling_window
            k = r.choice(keys)
            hw = b["schedule"]["hour_weights"]
            if kind == "hours_same" and k in hw:
                hw[k] = {str(h): float(w) for h, w in hw[k].items()}
            elif r.random() < 0.2 and k in hw:
                hw.pop(k)
            else:
                hw[k] = {str(h): r.choice([0, 0.5, 1, 2, 3.5])
                         for h in range(lo, hi) if r.random() < 0.8}
        elif kind == "lph":
            lo, hi = spec.dialling_window
            lph = b["schedule"]["lead_preferred_hour"]
            if lph and r.random() < 0.3:
                lph.pop(r.choice(list(lph)))
            else:
                lph[f"{spec.workflow_id}:p{r.randint(0, 99999):07d}"] = r.randint(lo, hi - 1)


# ---------------------------------------------------------------- parsing the simulator's 403
SHIFT_RE = re.compile(r"allocation shift ([0-9.]+) exceeds max_shift")
DIMS_RE = re.compile(r"dims not permitted for auto-apply: \[(.*)\]")


def parse_problems(problems: list[str]) -> dict:
    out = {"enabled": True, "bad_dims": (), "shift": None, "no_change": False,
           "rationale_missing": False}
    for p in problems:
        if p == "auto-apply is not enabled for this workflow":
            out["enabled"] = False
        elif m := DIMS_RE.match(p):
            out["bad_dims"] = tuple(x.strip().strip("'") for x in m.group(1).split(",") if x)
        elif m := SHIFT_RE.match(p):
            out["shift"] = float(m.group(1))
        elif p == "no change":
            out["no_change"] = True
        elif p == "rationale is required":
            out["rationale_missing"] = True
        else:
            raise AssertionError(f"unknown simulator problem {p!r}")
    return out


# ---------------------------------------------------------------- the test
def test_replica_agrees_with_simulator(sim):
    rng = random.Random(SEED)
    specs = {wf: sim.workflow(wf) for wf in POLICIES}
    gens = {wf: PairGen(rng, s) for wf, s in specs.items()}
    stats: Counter = Counter()
    disagreements: list[str] = []
    for i in range(N_PAIRS):
        wf = list(POLICIES)[i % len(POLICIES)]
        spec = specs[wf]
        snap = sim.config(wf)
        live = snap.config
        body = gens[wf].body(live)
        try:
            proposed = Config.from_body(body)
            invalid = proposed.problems(spec)
        except ConfigInvalid as e:
            invalid = list(e.problems)
        if invalid:          # we predict a 422 (no record, no log): check that and move on
            with pytest.raises(E.InvalidConfig):
                sim.auto_apply(wf, {"proposal_id": f"diff-{i:05d}", "base_version": snap.version,
                                    "proposed_config": body, "rationale": "x"})
            stats["422_predicted"] += 1
            continue
        aa = spec.governance.auto_apply
        rationale = "" if rng.random() < 0.02 else "differential test"
        exact = sim_verdict(aa, live, proposed, rationale)
        ours = sim_verdict(aa, live, proposed, rationale, margin=1e-6)
        assert abs(exact.shift - max_shift(live, proposed)) == 0.0
        req = {"proposal_id": f"diff-{i:05d}", "base_version": snap.version,
               "proposed_config": body, "rationale": rationale}
        try:
            res = sim.auto_apply(wf, req)
        except E.AutoApplyNotPermitted as e:
            stats["403"] += 1
            got = parse_problems(e.problems)
            want = {"enabled": exact.enabled, "bad_dims": exact.bad_dims,
                    "no_change": exact.no_change, "rationale_missing": exact.rationale_missing}
            for k, v in want.items():
                if got[k] != v:
                    disagreements.append(f"{wf} #{i}: {k} sim={got[k]} ours={v}")
            if got["shift"] is None:
                if exact.shift_exceeds:
                    disagreements.append(f"{wf} #{i}: we predict shift {exact.shift} exceeds")
            else:
                stats["403_shift"] += 1
                if not exact.shift_exceeds or abs(got["shift"] - exact.shift) > 5e-4 + 1e-9:
                    disagreements.append(f"{wf} #{i}: shift sim={got['shift']} ours={exact.shift}")
            if exact.permitted:
                disagreements.append(f"{wf} #{i}: predicted permitted, got 403 {e.problems}")
            if ours.permitted:
                disagreements.append(f"{wf} #{i}: OUR RULE PERMITTED A 403 {e.problems}")
            if not aa.enabled:
                stats["exact_changed_dims_checked"] += 1
            continue
        except E.InvalidConfig as e:
            disagreements.append(f"{wf} #{i}: 422 on a locally valid body: {e.problems}")
            continue
        stats["200"] += 1
        if not exact.permitted:
            disagreements.append(f"{wf} #{i}: predicted 403 {exact.problems(aa.max_shift)}, got 200")
        if tuple(res.changed_dims) != exact.changed_dims:
            disagreements.append(f"{wf} #{i}: changed_dims sim={res.changed_dims} "
                                 f"ours={exact.changed_dims}")
        stats["exact_changed_dims_checked"] += 1
        stored = sim.config(wf).config
        if stored != proposed:
            disagreements.append(f"{wf} #{i}: stored config differs from our normalisation")
        if exact.shift > aa.max_shift - 1e-6 and exact.shift > 0:
            stats["boundary_accepted_by_sim_refused_by_us"] += 1
    checked = stats["200"] + stats["403"]
    print(dict(stats))
    assert not disagreements, "\n".join(disagreements[:30])
    assert checked >= 2000
    assert stats["200"] >= 100 and stats["403_shift"] >= 100
    assert stats["exact_changed_dims_checked"] >= 600
