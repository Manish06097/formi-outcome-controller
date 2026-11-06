"""Write the private scenario variants (DESIGN.md 17 override 3, 10.1 "E2E custom" and 9.7).

Test tooling only. This script reads the public scenario YAMLs and writes deterministic variants
to `scenarios/private/`; the controller never reads either (CLAUDE.md hard rules). Thresholds
marked `[T]` in DESIGN.md may be tuned only against these files, never against public truth.

Every variant is a pure function of its base file: no randomness, fixed seeds, keys written in a
fixed order, so re-running the script reproduces the committed files byte for byte (the test
`tests/variants/test_variants.py` checks that). Each file starts with a header comment saying
what it tests, what a passing controller does on it, and exactly what changed from the base.

    .venv/bin/python scripts/variants/make_variants.py            # (re)write scenarios/private/
    .venv/bin/python scripts/variants/make_variants.py --check    # exit 1 if a file would change
    .venv/bin/python scripts/variants/make_variants.py --verify   # also load + baseline each one,
                                                                  # writing the wall-time table

Adding a variant: append a `Variant(...)` to `VARIANTS` (or a family in `families()`), run the
script, run `--verify`, commit the YAML with the script change.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "scenarios"
PRIVATE = PUBLIC / "private"
TIMINGS = ROOT / "docs" / "measurements" / "private-variants-baseline.md"

BASES = {
    "p01": "public_01_dev.yaml",
    "p02": "public_02_incidents.yaml",
}

PUBLIC_KEYS = ("name", "seed", "days", "history_days", "tick_minutes", "credits", "tenants",
               "prompts")
"""Keys a controller could learn from the API (in this order); everything else is hidden truth."""

ENGAGE_WFS_P01 = ("nw_renewal", "ks_cancel")
"""Engagement-primary workflows of the public bases (read from the YAML objective below too)."""

# Prompts added by variants. Hallucination rates match the rows of DESIGN.md 7.1 that the
# verdict and CUSUM tables were computed for (P4-like 0.0275, P2-like 0.0545), so the variants
# exercise exactly the cases the design claims numbers for.
EXTRA_PROMPTS = {
    "P6": {"summary": "Supervisor edit: urgency language and stronger benefit claims",
           "halluc": 0.0545, "engage_mult": 0.93},
    "P7": {"summary": "Supervisor edit: warmer tone and freer benefit explanations",
           "halluc": 0.0275, "engage_mult": 1.05},
    "P8": {"summary": "Business default: long-form persuasive script",
           "halluc": 0.03, "engage_mult": 1.04},
}


# =============================================================================== edit context

class Ctx:
    """A scenario being edited, plus the human-readable list of changes for the header."""

    def __init__(self, sc: dict[str, Any]):
        self.sc = sc
        self.changes: list[str] = []

    def note(self, text: str) -> None:
        self.changes.append(text)

    # ------------------------------------------------------------------ lookups
    def workflows(self) -> list[dict[str, Any]]:
        return [wf for t in self.sc["tenants"] for wf in t["workflows"]]

    def wf(self, wid: str) -> dict[str, Any]:
        for w in self.workflows():
            if w["id"] == wid:
                return w
        raise KeyError(wid)

    def engagement_wfs(self) -> list[str]:
        return [w["id"] for w in self.workflows() if w["objective"]["primary"] == "engagement_rate"]

    # ------------------------------------------------------------------ edits
    def set_top(self, key: str, value: Any) -> None:
        old = self.sc.get(key)
        self.sc[key] = value
        self.note(f"{key}: {old} -> {value}")

    def set_gov(self, field_: str, value: Any, wfs: Iterable[str] | None = None) -> None:
        ids = list(wfs) if wfs is not None else [w["id"] for w in self.workflows()]
        for wid in ids:
            self.wf(wid).setdefault("governance", {})[field_] = copy.deepcopy(value)
        self.note(f"governance.{field_} = {_flow(value)} on {', '.join(ids)}")

    def set_auto_apply(self, wfs: Iterable[str] | None = None, **kw: Any) -> None:
        ids = list(wfs) if wfs is not None else [w["id"] for w in self.workflows()]
        for wid in ids:
            aa = self.wf(wid).setdefault("governance", {}).setdefault(
                "auto_apply", {"enabled": False, "allowed_dims": [], "max_shift": 0.0})
            aa.update(copy.deepcopy(kw))
        self.note(f"governance.auto_apply {_flow(kw)} on {', '.join(ids)}")

    def set_wf(self, wid: str, key: str, value: Any) -> None:
        old = self.wf(wid).get(key)
        self.wf(wid)[key] = copy.deepcopy(value)
        self.note(f"{wid}.{key}: {_flow(old)} -> {_flow(value)}")

    def set_default_alloc(self, wid: str, dim: str, dist: dict[str, float]) -> None:
        self.wf(wid)["default_config"]["allocations"]["default"][dim] = dict(dist)
        self.note(f"{wid} default_config {dim} = {_flow(dist)}")

    def set_default_prompt(self, wid: str, prompt: str) -> None:
        self.ensure_option(wid, "prompt_version", prompt)
        self.wf(wid)["default_config"]["prompt_version"] = prompt
        self.note(f"{wid} default_config prompt_version = {prompt} "
                  f"(halluc {self.sc['prompts'][prompt]['halluc']})")

    def add_prompt(self, name: str) -> None:
        if name not in self.sc["prompts"]:
            self.sc["prompts"][name] = dict(EXTRA_PROMPTS[name])
            p = EXTRA_PROMPTS[name]
            self.note(f"prompt catalog + {name} (halluc {p['halluc']}, engage_mult "
                      f"{p['engage_mult']})")

    def ensure_option(self, wid: str, dim: str, option: str) -> None:
        if dim == "prompt_version" and option in EXTRA_PROMPTS:
            self.add_prompt(option)
        opts = self.wf(wid)["options"][dim]
        if option not in opts:
            opts.append(option)
            self.note(f"{wid}.options.{dim} + {option}")

    def did_health(self, wid: str, did: str, health: float) -> None:
        self.wf(wid).setdefault("did_base_health", {})[did] = health
        self.note(f"{wid} hidden did_base_health {did} = {health}")

    def clear_incidents(self, keep: Callable[[dict[str, Any]], bool] = lambda _i: False) -> None:
        before = len(self.sc.get("incidents", []))
        self.sc["incidents"] = [i for i in self.sc.get("incidents", []) if keep(i)]
        removed = before - len(self.sc["incidents"])
        if removed:
            self.note(f"removed {removed} base incident(s)")

    def add_incident(self, inc: dict[str, Any]) -> None:
        self.sc.setdefault("incidents", []).append(inc)
        self.sc["incidents"].sort(key=lambda i: _minute(i["at"]))
        what = (f"supervisor_edit {_flow(inc['patch'])}" if inc["type"] == "supervisor_edit"
                else f"did_flagged {inc['did']} -> {inc['base_health']}")
        self.note(f"incident at {inc['at']} on {inc['workflow']}: {what}")

    def set_effects(self, effects: list[dict[str, Any]], why: str) -> None:
        self.sc["effects"] = effects
        self.note(f"effects replaced: {why}")

    def scale_volume(self, wid: str, factor: float) -> None:
        w = self.wf(wid)
        w["dials_per_hour"] = int(round(w["dials_per_hour"] * factor))
        w["daily_batch"]["size"] = int(round(w["daily_batch"]["size"] * factor))
        self.note(f"{wid} volume x{factor}: dials_per_hour {w['dials_per_hour']}, daily batch "
                  f"{w['daily_batch']['size']}")


def _flow(v: Any) -> str:
    """Compact one-line YAML-ish rendering for header notes."""
    return json.dumps(v, sort_keys=True, separators=(", ", ": ")) if isinstance(v, (dict, list)) \
        else str(v)


def _minute(when: str | int) -> int:
    if isinstance(when, int):
        return when
    day, hm = when.split()
    h, m = hm.split(":")
    return int(day.lstrip("Dd")) * 1440 + int(h) * 60 + int(m)


# =============================================================================== effect helpers

def _round_mult(x: float) -> float:
    return float(f"{x:.4f}")


def flip_effects(effects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reverse every effect's sign on the log scale (mult -> 1/mult, decay targets too)."""
    out = copy.deepcopy(effects)
    for e in out:
        e["mult"] = _round_mult(1.0 / e["mult"])
        if e.get("decay"):
            e["decay"]["to_mult"] = _round_mult(1.0 / e["decay"]["to_mult"])
    return out


def resize_effects(effects: list[dict[str, Any]], k: float) -> list[dict[str, Any]]:
    """Scale every effect's log-multiplier by k (k < 1 shrinks, k > 1 grows)."""
    out = copy.deepcopy(effects)
    for e in out:
        e["mult"] = _round_mult(math.exp(k * math.log(e["mult"])))
        if e.get("decay"):
            e["decay"]["to_mult"] = _round_mult(math.exp(k * math.log(e["decay"]["to_mult"])))
    return out


def last_listed_signflip(ctx: Ctx) -> list[dict[str, Any]]:
    """J2 case (j): the only winners are the LAST-listed welcome and voice options, and each one
    helps one cohort while hurting another. Every other non-control option is slightly worse
    than control, so an option-order queue or a population-only test finds nothing good."""
    effects: list[dict[str, Any]] = []
    for wid in ctx.engagement_wfs():
        w = ctx.wf(wid)
        langs = list(w["language_mix"])
        for dim in ("welcome_message", "voice"):
            opts = w["options"][dim]
            control = w["default_config"]["allocations"]["default"][dim]
            ctrl = next(iter(control))
            for o in opts[:-1]:
                if o != ctrl:
                    effects.append({"workflow": wid, "dim": dim, "value": o, "mult": 0.97})
            last = opts[-1]
            if dim == "voice":
                effects.append({"workflow": wid, "dim": dim, "value": last, "mult": 1.16,
                                "where": {"age_band": ["46-60", "60+"]}})
                effects.append({"workflow": wid, "dim": dim, "value": last, "mult": 0.88,
                                "where": {"age_band": ["18-30"]}})
            else:
                effects.append({"workflow": wid, "dim": dim, "value": last, "mult": 1.12,
                                "where": {"lang": [langs[0]]}})
                effects.append({"workflow": wid, "dim": dim, "value": last, "mult": 0.9,
                                "where": {"lang": [langs[-1]]}})
    return effects


# =============================================================================== variants

@dataclass
class Variant:
    name: str
    base: str
    tests: str
    expect: str
    design: str
    edit: Callable[[Ctx], None]
    family: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _seed(seed: int) -> Callable[[Ctx], None]:
    return lambda c: c.set_top("seed", seed)


def _v_effects_flipped(c: Ctx) -> None:
    c.set_effects(flip_effects(c.sc["effects"]), "every base effect sign-flipped (mult -> 1/mult)")


def _v_effects_small(c: Ctx) -> None:
    c.set_effects(resize_effects(c.sc["effects"], 0.5),
                  "every base effect halved on the log scale")


def _v_effects_large(c: Ctx) -> None:
    c.set_effects(resize_effects(c.sc["effects"], 1.6),
                  "every base effect x1.6 on the log scale")


def _v_flip_resize_02(c: Ctx) -> None:
    c.set_effects(resize_effects(flip_effects(c.sc["effects"]), 1.3),
                  "every base effect sign-flipped and x1.3 on the log scale")


def _v_last_listed(c: Ctx) -> None:
    c.set_effects(last_listed_signflip(c), "winners only among the last-listed welcome and voice "
                  "options, each helping one cohort and hurting another; every other "
                  "non-control option x0.97")


def _decay_effects(c: Ctx, wid: str) -> None:
    keep = [e for e in c.sc["effects"] if not (e["workflow"] == wid and e.get("decay"))]
    w3 = c.wf(wid)["options"]["welcome_message"][-1]
    keep.append({"workflow": wid, "dim": "welcome_message", "value": w3, "mult": 1.2,
                 "decay": {"after_exposure_days": 3, "to_mult": 0.9, "over_days": 2}})
    c.set_effects(keep, f"{wid} {w3} x1.20 decaying after 3 exposure days to x0.90 over 2 days "
                  "(other base effects kept)")


def _v_decay_highvol(c: Ctx) -> None:
    c.scale_volume("nw_renewal", 3.0)
    _decay_effects(c, "nw_renewal")


def _v_decay_dev(c: Ctx) -> None:
    _decay_effects(c, "nw_renewal")


def _v_did_flag_d1(c: Ctx) -> None:
    c.add_incident({"type": "did_flagged", "workflow": "ks_cancel", "did": "D2",
                    "at": "D1 10:00", "base_health": 0.35})


def _v_did_flag_d1_sole(c: Ctx) -> None:
    c.add_incident({"type": "did_flagged", "workflow": "nw_kyc", "did": "D1",
                    "at": "D1 14:00", "base_health": 0.5})


def _v_preflagged_unknown(c: Ctx) -> None:
    c.did_health("nw_renewal", "D3", 0.3)
    c.did_health("ks_cancel", "D3", 0.3)


def _v_p4_like_supervisor(c: Ctx) -> None:
    c.ensure_option("ks_cancel", "prompt_version", "P7")
    c.add_incident({"type": "supervisor_edit", "workflow": "ks_cancel", "at": "D3 11:30",
                    "patch": {"prompt_version": "P7"},
                    "note": "supervisor ships a P4-like prompt (about 4.6x P1 hallucination)"})


def _v_p2_like_supervisor(c: Ctx) -> None:
    c.ensure_option("nw_renewal", "prompt_version", "P6")
    c.add_incident({"type": "supervisor_edit", "workflow": "nw_renewal", "at": "D2 14:00",
                    "patch": {"prompt_version": "P6"},
                    "note": "supervisor ships a P2-like prompt (about 9x P1 hallucination)"})


def _v_unsafe_default(c: Ctx) -> None:
    c.ensure_option("ks_cancel", "prompt_version", "P5")
    c.set_default_prompt("ks_cancel", "P4")
    c.set_default_prompt("nw_renewal", "P8")


def _v_two_incidents(c: Ctx) -> None:
    c.clear_incidents(keep=lambda i: i["type"] == "supervisor_edit")
    c.add_incident({"type": "did_flagged", "workflow": "ks_cancel", "did": "D2",
                    "at": "D3 13:30", "base_health": 0.35})


def _v_supervisor_during_pending(c: Ctx) -> None:
    c.add_incident({"type": "supervisor_edit", "workflow": "nw_renewal", "at": "D1 11:30",
                    "patch": {"allocations": {"default": {"voice": {"V2": 1}}}},
                    "note": "supervisor changes the default voice 30 minutes after the routine "
                            "slot, while our request is pending"})
    c.add_incident({"type": "supervisor_edit", "workflow": "ks_cancel", "at": "D2 11:45",
                    "patch": {"allocations": {"default": {"welcome_message": {"W2": 1}}}},
                    "note": "supervisor changes the default welcome while our request is pending"})


def _v_gov_a(c: Ctx) -> None:
    c.set_gov("proposal_ttl_hours", 12)
    c.set_gov("approver_latency_minutes", [60, 480])


def _v_gov_b(c: Ctx) -> None:
    c.set_gov("approver_hours", [14, 16])
    c.set_gov("proposal_ttl_hours", 4)
    c.set_gov("approver_latency_minutes", [30, 240])


def _v_gov_c(c: Ctx) -> None:
    c.set_gov("max_requests_per_24h", 1)
    c.set_auto_apply(enabled=False, allowed_dims=[], max_shift=0.0)


def _v_gov_d(c: Ctx) -> None:
    c.set_auto_apply(max_shift=0.05)
    c.set_default_alloc("ks_cancel", "caller_id", {"D1": 1, "D2": 1, "D3": 1})
    c.add_incident({"type": "did_flagged", "workflow": "ks_cancel", "did": "D2",
                    "at": "D2 10:00", "base_health": 0.35})


def _v_prompt_auto(c: Ctx) -> None:
    for w in c.workflows():
        aa = w["governance"]["auto_apply"]
        if aa.get("enabled"):
            aa["allowed_dims"] = list(aa["allowed_dims"]) + ["prompt_version"]
    c.note("governance.auto_apply.allowed_dims + prompt_version on every workflow with "
           "auto-apply enabled (nw_renewal, ks_cancel)")


def _v_tick5(c: Ctx) -> None:
    c.set_top("tick_minutes", 5)


def _credits(n: int) -> Callable[[Ctx], None]:
    return lambda c: c.set_top("credits", n)


def _v_reject60(c: Ctx) -> None:
    c.set_gov("base_reject_rate", 0.6)


def _v_tick30(c: Ctx) -> None:
    c.set_top("tick_minutes", 30)


def _v_apply_window_1h(c: Ctx) -> None:
    c.set_gov("apply_window_hours", 1)


def _v_max_shift_0(c: Ctx) -> None:
    c.set_auto_apply(max_shift=0.0)


def _v_auto_off(c: Ctx) -> None:
    c.set_auto_apply(enabled=False, allowed_dims=[], max_shift=0.0)


def _v_history_0(c: Ctx) -> None:
    c.set_top("history_days", 0)


def _v_gate_099(c: Ctx) -> None:
    c.set_gov("min_validation_pass_rate", 0.99)


def _v_next_campaign_all(c: Ctx) -> None:
    for w in c.workflows():
        w["mid_flight"] = "next_campaign_only"
    c.note("mid_flight = next_campaign_only on every workflow")


def _v_approvers_14_16_ttl24(c: Ctx) -> None:
    c.set_gov("approver_hours", [14, 16])
    c.set_gov("proposal_ttl_hours", 24)
    c.set_gov("approver_latency_minutes", [30, 240])


GOV_EXPECT = ("0 auto_apply_rejected, 0 lapsed, 0 expired, 0 apply conflicts, peak requests per "
              "24h at most C_eff, 0 harmful")

VARIANTS: list[Variant] = [
    # ------------------------------------------------------------------ seeds
    Variant("seed_p01_s1101", "p01", "the public_01 world with a new seed: different leads, "
            "archetypes, approver coin flips and validation outcomes, same truth",
            GOV_EXPECT + "; engagement gap captured above 0", "17 override 3", _seed(1101)),
    Variant("seed_p01_s1102", "p01", "as seed_p01_s1101 with another seed",
            GOV_EXPECT + "; engagement gap captured above 0", "17 override 3", _seed(1102)),
    Variant("seed_p02_s2201", "p02", "the public_02 world (DID flag, P2 supervisor prompt, "
            "supervisor voice edit) with a new seed", GOV_EXPECT + "; hallucination MTTR under "
            "4h; flagged-DID share 0 the day after the flag", "17 override 3, 7.4", _seed(2201)),
    Variant("seed_p02_s2202", "p02", "as seed_p02_s2201 with another seed",
            GOV_EXPECT + "; hallucination MTTR under 4h", "17 override 3", _seed(2202)),
    # ------------------------------------------------------------------ effects
    Variant("effects_flipped_p01", "p01", "every public_01 effect sign-flipped: yesterday's "
            "winners are today's losers, so nothing learned on public truth transfers",
            "0 harmful; no promotion of a flipped-negative option; gap captured above 0 where a "
            "positive effect exists", "5.4, 5.5, UNDERSTANDING 7", _v_effects_flipped),
    Variant("effects_small_p01", "p01", "every public_01 effect halved: winners near the "
            "detection limit, the false-winner and futility regime", "0 harmful; no winner "
            "declared on an option whose true effect is below the harm line", "5.5",
            _v_effects_small),
    Variant("effects_large_p01", "p01", "every public_01 effect x1.6 on the log scale: big wins "
            "and big losses, so exploration arms on the losers are near the 1% harm line",
            "0 harmful exploration arms; winners found sooner than on public_01", "5.4",
            _v_effects_large),
    Variant("effects_flip_resize_p02", "p02", "public_02 effects sign-flipped and x1.3, with the "
            "public_02 incidents unchanged", GOV_EXPECT + "; MTTR under 4h", "5.4, 7",
            _v_flip_resize_02),
    Variant("last_listed_signflip_p01", "p01", "J2 case (j): winners only among the last-listed "
            "options, with sign-flipping cohort effects (an option-order queue tests them last; "
            "a population test sees them diluted)", "engagement gap captured above 0, 0 harmful, "
            "no cohort's predicted de_k below -x_k at any applied change", "10.1 (j), 5.3, 5.4",
            _v_last_listed),
    # ------------------------------------------------------------------ decay
    Variant("decaying_winner_highvol_p01", "p01", "J2 case (g): a strong winner (W3 x1.20) that "
            "decays after 3 exposure days to x0.90, on nw_renewal at 3x volume so the ramp is "
            "fast and the stationary posterior is very confident when decay starts",
            "no ramp or promotion graded harmful; decayed-variant share under 10% by onset + 2 "
            "days", "10.1 (g), 5.5, 5.7", _v_decay_highvol),
    Variant("decaying_winner_dev_p01", "p01", "J2 case (g) at dev volume: the same decaying W3 "
            "on nw_renewal at public volume", "no ramp or promotion graded harmful; decay rolled "
            "back without a harmful rollback at onset", "5.7", _v_decay_dev),
    # ------------------------------------------------------------------ caller ID
    Variant("did_flag_d1_p01", "p01", "a caller ID (ks_cancel D2, half the traffic) flagged on "
            "D1 10:00, before most probes have classified the spare DID", "flagged-DID share on "
            "the flag day within the 7.3 bound plus 0.01, 0 the day after; no change with dd "
            "below -2%", "7.3, 5.9", _v_did_flag_d1),
    Variant("did_flag_d1_sole_p01", "p01", "the sole caller ID of nw_kyc flagged at D1 14:00: no "
            "concurrent comparator, so only the KPI drift detector can see it", "KPI_DRIFT "
            "raised, probes start, then traffic moves to a healthy DID by approval (KYC has "
            "auto-apply off)", "7.5, 5.9", _v_did_flag_d1_sole),
    Variant("preflagged_unknown_did_p01", "p01", "J2 case (h): spare caller IDs that are already "
            "flagged (health 0.3) and never used by the default config: probing them is "
            "graded on DID health", "no change with dd below -2%; the bad DID is classified "
            "unhealthy from probe dials and never spread onto", "10.1 (h), 5.9",
            _v_preflagged_unknown),
    Variant("max_shift_005_did_third_p01", "p01", "J2 case (d): max_shift 0.05 on every "
            "auto-apply policy and a caller ID flagged at a one-third share (ks_cancel default "
            "split over D1, D2, D3)", "flag-day share at most 0.096 + 0.01 (7.3 formula for "
            "s = 0.333), 0 the day after; every chain step within max_shift",
            "10.1 (d), 6.5, 7.3", _v_gov_d),
    # ------------------------------------------------------------------ prompts
    Variant("p4_like_supervisor_p01", "p01", "J2 case (f): the Kestrel supervisor ships a "
            "P4-like prompt (halluc 0.0275, engage x1.05) that passes the client gate about 69% "
            "of the time and LOOKS better on engagement", "MTTR finite (a fix with a FULL "
            "verdict applied); never P5 for Kestrel; never a cherry-picked gate run",
            "10.1 (f), 7.1", _v_p4_like_supervisor),
    Variant("p2_like_supervisor_p01", "p01", "a Northwind supervisor ships a P2-like prompt "
            "(halluc 0.0545, engage x0.93) on nw_renewal at D2 14:00", "MTTR under 4h; fix by "
            "last known good (P1) through an approval with evidence", "7.1",
            _v_p2_like_supervisor),
    Variant("unsafe_default_prompt_p01", "p01", "J2 case (i): the business default prompt is "
            "unsafe on two tenants (ks_cancel P4, nw_renewal a new P8 at halluc 0.03); no "
            "supervisor event and no relative detector can see it", "D0 safety screen finds "
            "both; fix proposed with a FULL verdict and a designated gate run; never the strict "
            "script P5 on Kestrel", "10.1 (i), 7.1 F7", _v_unsafe_default),
    Variant("two_incidents_p02", "p02", "J2 case (e): a caller ID is flagged at D3 13:30 on "
            "ks_cancel while the prompt-fix approval for the D3 11:30 P2 edit is pending",
            "the prompt-fix approval is never staled or superseded; the drain starts when it is "
            "terminal (or is bundled); MTTR under 4h", "10.1 (e), 6.2, 7.3", _v_two_incidents),
    Variant("supervisor_edit_pending_p01", "p01", "supervisor edits land while our routine "
            "approval is pending (nw_renewal D1 11:30, ks_cancel D2 11:45)", "our pending "
            "proposal goes STALE, is rebased and re-queued; the supervisor values are never "
            "reverted without an approval; 0 apply conflicts", "6.7, 6.1", _v_supervisor_during_pending),
    # ------------------------------------------------------------------ governance attacks
    Variant("gov_a_ttl12_lat480_p01", "p01", "J2 case (a): TTL 12h with approver latency 60-480 "
            "on every workflow, so most of the day has no expiry-safe submit minute",
            GOV_EXPECT + "; no expiry; at least one approval applied per day", "10.1 (a), 6.3",
            _v_gov_a),
    Variant("gov_b_approvers_14_16_ttl4_p01", "p01", "J2 case (b): approvers 14-16 with TTL 4h "
            "and latency 30-240: the only safe submit is before the approvers open",
            GOV_EXPECT + "; no expiry; at least one approval applied per day", "10.1 (b), 6.3, 9.7",
            _v_gov_b),
    Variant("gov_c_cap1_p01", "p01", "J2 case (c): fatigue cap 1 per 24h and auto-apply off "
            "everywhere", GOV_EXPECT + "; the D0 foundation approval ships on D0; routine at most "
            "once per 48h", "10.1 (c), 6.4 F2", _v_gov_c),
    Variant("gov_prompt_auto_allowed_p02", "p02", "policy lets prompt_version auto-apply on the "
            "two auto-apply workflows, and public_02 has a P2 supervisor prompt incident",
            GOV_EXPECT + "; the stricter of policy and directives decides; a prompt is never "
            "changed without a SAFE verdict", "6.5, 7.1", _v_prompt_auto),
    Variant("tick5_p01", "p01", "tick_minutes 5: three times as many decision points, a "
            "visibility lag of 5 to 9 minutes", GOV_EXPECT + "; every advance a multiple of 5",
            "4.2, 9.7", _v_tick5),
    Variant("credits50_p02", "p02", "credits 50 with the public_02 incidents: R = 40 < W = 65, "
            "LOW credit mode from the start", GOV_EXPECT + "; never a 402; stage-1-only verdicts; "
            "the hallucination incident still fixed", "7.2, 9.7", _credits(50)),
    Variant("credits60_p02", "p02", "credits 60 with the public_02 incidents: just above the "
            "credits-50 case", GOV_EXPECT + "; never a 402; credits above the incident reserve",
            "7.2", _credits(60)),
    Variant("reject60_p01", "p01", "J2 case (k): the approver rejects 60% of requests",
            GOV_EXPECT + "; rejected work re-queued with fresh data; fatigue never exceeded",
            "10.1 (k), 6.1", _v_reject60),
    # ------------------------------------------------------------------ 9.7 constraint rows
    Variant("tick30_p02", "p02", "9.7 row: tick_minutes 30", GOV_EXPECT + "; MTTR under 4h",
            "9.7, 4.2", _v_tick30),
    Variant("apply_window_1h_p01", "p01", "9.7 row: apply_window_hours 1 everywhere",
            GOV_EXPECT + "; 0 lapsed (steps of at most S_watch_max - tick)", "9.7, 4.2",
            _v_apply_window_1h),
    Variant("max_shift_0_p02", "p02", "9.7 row: max_shift 0 on every auto-apply policy, with "
            "the public_02 DID flag", GOV_EXPECT + "; drains become P0 approvals", "9.7, 6.5",
            _v_max_shift_0),
    Variant("auto_apply_off_p02", "p02", "9.7 row: auto-apply disabled everywhere, with the "
            "public_02 incidents", GOV_EXPECT + "; the DID drain is a P0 approval, bundled with "
            "the prompt fix if both are open", "9.7, 6.5", _v_auto_off),
    Variant("history0_p01", "p01", "9.7 row: history_days 0 (no history chunks at all)",
            GOV_EXPECT + "; no schedule change for the first 3 dialling days", "9.7, C3",
            _v_history_0),
    Variant("gate099_p02", "p02", "9.7 row: min_validation_pass_rate 0.99, a tight client gate",
            GOV_EXPECT + "; no safe prompt declared UNSAFE; designated evidence run at n = 100",
            "9.7, 7.1", _v_gate_099),
    Variant("next_campaign_only_p01", "p01", "9.7 row: mid_flight next_campaign_only on every "
            "workflow", GOV_EXPECT + "; verification counts new campaigns only", "9.7, 6.6",
            _v_next_campaign_all),
    Variant("approvers_14_16_ttl24_p01", "p01", "9.7 row: approvers 14-16 with TTL 24h and "
            "latency 30-240", GOV_EXPECT + "; routine slot 14:30", "9.7, 6.3",
            _v_approvers_14_16_ttl24),
]


def families() -> list[Variant]:
    """Seed families: (f) needs MTTR finite in at least 18 of 20 seeds (DESIGN.md 10.1, M6)."""
    out: list[Variant] = []
    for i in range(20):
        seed = 6001 + i

        def edit(c: Ctx, seed: int = seed) -> None:
            _v_p4_like_supervisor(c)
            c.set_top("seed", seed)

        out.append(Variant(f"p4_like_supervisor_p01_s{seed}", "p01",
                           f"J2 case (f) seed family member {i + 1} of 20 (seed {seed})",
                           "MTTR finite in at least 18 of the 20 family members",
                           "10.1 (f), 15.1 M6", edit, family="p4_like_seeds"))
    return out


def all_variants() -> list[Variant]:
    return VARIANTS + families()


# =============================================================================== rendering

def build(v: Variant) -> tuple[dict[str, Any], list[str]]:
    base = yaml.safe_load((PUBLIC / BASES[v.base]).read_text())
    ctx = Ctx(copy.deepcopy(base))
    ctx.sc["name"] = v.name
    v.edit(ctx)
    return ctx.sc, ctx.changes


def render(v: Variant) -> str:
    sc, changes = build(v)
    header = [
        f"# Private variant: {v.name}",
        f"# Generated from scenarios/{BASES[v.base]} by scripts/variants/make_variants.py; "
        "do not edit by hand.",
        f"# Tests: {v.tests}",
        f"# Pass: {v.expect}",
        f"# Design: DESIGN.md {v.design}",
        "# Changes from the base:",
        *[f"#   - {c}" for c in changes],
        "# Only tests and harness scripts read this file; the controller never does.",
    ]
    wrapped: list[str] = []
    for line in header:
        wrapped.extend(_wrap_comment(line))
    public = {k: sc[k] for k in PUBLIC_KEYS if k in sc}
    hidden = {k: v_ for k, v_ in sc.items() if k not in PUBLIC_KEYS}
    body = yaml.safe_dump(public, sort_keys=False, default_flow_style=None, width=100)
    body += "# ---- hidden ground truth (do not read from your controller) ----\n"
    body += yaml.safe_dump(hidden, sort_keys=False, default_flow_style=None, width=100)
    return "\n".join(wrapped) + "\n" + body


def _wrap_comment(line: str, width: int = 100) -> list[str]:
    if len(line) <= width:
        return [line]
    indent = "#     " if line.startswith("#   - ") else "#   "
    words = line.split(" ")
    out, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            out.append(cur)
            cur = indent + w
        else:
            cur = f"{cur} {w}" if cur else w
    out.append(cur)
    return out


def index_entry(v: Variant) -> dict[str, Any]:
    return {"name": v.name, "file": f"scenarios/private/{v.name}.yaml",
            "base": f"scenarios/{BASES[v.base]}", "tests": v.tests, "pass": v.expect,
            "design": v.design, "family": v.family}


def write_all(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    names = set()
    for v in all_variants():
        if v.name in names:
            raise SystemExit(f"duplicate variant name {v.name}")
        names.add(v.name)
        p = out_dir / f"{v.name}.yaml"
        p.write_text(render(v))
        paths.append(p)
    index = [index_entry(v) for v in all_variants()]
    (out_dir / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    stale = [p for p in out_dir.glob("*.yaml") if p.stem not in names]
    for p in stale:
        p.unlink()
    return paths


# =============================================================================== verification

def verify(paths: list[Path], python: str) -> list[dict[str, Any]]:
    """Load every file with sim.scenario.load_scenario and run `python -m sim baseline` on it."""
    sys.path.insert(0, str(ROOT))
    from sim.scenario import load_scenario  # test tooling may import sim

    rows = []
    for p in paths:
        sc = load_scenario(p)
        with tempfile.TemporaryDirectory(prefix="variant_base_") as tmp:
            t0 = time.monotonic()
            r = subprocess.run([python, "-m", "sim", "baseline", "--scenario", str(p),
                                "--run-dir", str(Path(tmp) / "b")], cwd=ROOT,
                               capture_output=True, text=True)
            wall = time.monotonic() - t0
        ok = r.returncode == 0
        events = None
        if ok and "events" in r.stdout:
            events = int(r.stdout.strip().split(",")[-1].split()[0])
        rows.append({"name": p.stem, "ok": ok, "wall_s": round(wall, 1), "events": events,
                     "days": sc["days"], "history_days": sc["history_days"],
                     "tick": sc["tick_minutes"], "stderr": r.stderr[-400:] if not ok else ""})
        print(f"{p.stem:42s} {'ok' if ok else 'FAIL':4s} {wall:6.1f}s events={events}",
              flush=True)
    return rows


def write_timings(rows: list[dict[str, Any]]) -> None:
    import platform
    lines = [
        "# Private scenario variants: simulator-only baseline cost",
        "",
        f"Generated by `scripts/variants/make_variants.py --verify` on {time.strftime('%Y-%m-%d')}"
        f" ({platform.machine()}, Python {platform.python_version()}). Each file was loaded with "
        "`sim.scenario.load_scenario` and run with `python -m sim baseline` (no controller, "
        "no HTTP, daily advances). Wall time includes interpreter start and history generation.",
        "",
        "| Variant | Sim days (history + run) | Tick | Events | Baseline wall (s) | Result |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['name']} | {r['history_days']} + {r['days']} | {r['tick']} | "
                     f"{r['events'] if r['events'] is not None else '-'} | {r['wall_s']} | "
                     f"{'ok' if r['ok'] else 'FAIL'} |")
    total = sum(r["wall_s"] for r in rows)
    lines += ["", f"Total: {len(rows)} variants, {total:.0f} s of baseline wall time, "
              f"{sum(1 for r in rows if not r['ok'])} failures.", ""]
    TIMINGS.parent.mkdir(parents=True, exist_ok=True)
    TIMINGS.write_text("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(PRIVATE))
    ap.add_argument("--check", action="store_true", help="fail if any file would change")
    ap.add_argument("--verify", action="store_true",
                    help="load and baseline every variant; write the wall-time table")
    ap.add_argument("--python", default=sys.executable)
    a = ap.parse_args(argv)
    out = Path(a.out)
    if a.check:
        with tempfile.TemporaryDirectory() as tmp:
            fresh = write_all(Path(tmp))
            files = [p.name for p in fresh] + ["index.json"]
            bad = [n for n in files if not (out / n).exists()
                   or (out / n).read_text() != (Path(tmp) / n).read_text()]
            names = {p.name for p in fresh}
            bad += [p.name + " (stale)" for p in out.glob("*.yaml") if p.name not in names]
        if bad:
            print("out of date: " + ", ".join(bad))
            return 1
        print(f"{len(fresh)} variants up to date")
        return 0
    paths = write_all(out)
    print(f"wrote {len(paths)} variants to {out}")
    if a.verify:
        rows = verify(paths, a.python)
        write_timings(rows)
        if not all(r["ok"] for r in rows):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
