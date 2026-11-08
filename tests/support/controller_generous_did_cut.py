"""`python -m controller` with the pickup learner's caller-ID cut made too generous (test tool).

`DidHealthModel.gate` trims a caller-ID change that fails its DID line to the largest step that
passes (ADR 0030). This wrapper lets such a cut overshoot by half again (a change that passes
whole is left alone), so a spread among SPRT-cleared DIDs reaches the release train with a DID
line that fails, exactly as nw_renewal v8 did on public_02 when the learner's cut sat on the edge
of the line on its own draws. The release train's composed gate (loop/engagement.py `gate`,
DESIGN 0.1) must then refuse it on its own. Used only by tests/e2e/test_gate_did_live.py.
"""
import sys

from controller.learning import did as did_module

_orig = did_module.DidHealthModel.gate
OVERSHOOT = 1.5


def _gate_without_cut(self, ws, cur, target, states):
    out, ev = _orig(self, ws, cur, target, states)
    if ev["lam"] >= 1.0 or ev["lam"] <= 0.0:
        return out, ev
    lam = min(1.0, ev["lam"] * OVERSHOOT)
    names = set(cur) | set(target)
    over = {d: cur.get(d, 0.0) + lam * (target.get(d, 0.0) - cur.get(d, 0.0)) for d in names}
    return {d: w for d, w in over.items() if w > 1e-12}, {**ev, "lam": lam, "overshoot_by_test": True}


did_module.DidHealthModel.gate = _gate_without_cut

from controller.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
