"""Load `controller/directives.yaml` and resolve the directive layer for one workflow.

Owner: M0. Implements DESIGN.md 6.5 (directives, scoped by tenant and use case). The result is
`types.ResolvedDirective`; `world/registry.py` (track T2) combines it with the API policy into
`EffectivePolicy`, taking the stricter value of each (a directive can only tighten).

Directives are client agreements from the memos, not simulator data, so reading this file does
not break the "only HTTP" rule: it is our own configuration, like settings.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import yaml

from controller.settings import Thresholds
from controller.types import FrozenDict, ResolvedDirective, freeze, thaw

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "directives.yaml"

CUSTOMER_FACING_AUTO = ("none", "risk_reducing_only", "ramps_and_risk_reducing")
NOTICE_SCOPES = ("customer_facing", "non_incident")
PRE_OPEN = ("only_if_no_safe_in_hours", "never")


class DirectiveError(ValueError):
    """directives.yaml is malformed."""


def _hhmm(v: Any) -> int | None:
    if v is None:
        return None
    try:
        h, m = str(v).split(":")
        hh, mm = int(h), int(m)
    except ValueError as e:
        raise DirectiveError(f"not_before must be 'HH:MM', got {v!r}") from e
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise DirectiveError(f"not_before out of range: {v!r}")
    return hh * 60 + mm


def _enum(choices: tuple[str, ...]) -> Callable[[Any], str]:
    def check(v: Any) -> str:
        if v not in choices:
            raise DirectiveError(f"expected one of {choices}, got {v!r}")
        return str(v)
    return check


def _int_ge(lo: int, nullable: bool = False) -> Callable[[Any], int | None]:
    def check(v: Any) -> int | None:
        if v is None and nullable:
            return None
        if isinstance(v, bool) or not isinstance(v, int) or v < lo:
            raise DirectiveError(f"expected an int >= {lo}, got {v!r}")
        return v
    return check


def _bool(v: Any) -> bool:
    if not isinstance(v, bool):
        raise DirectiveError(f"expected a bool, got {v!r}")
    return v


def _terms(v: Any) -> tuple[str, ...]:
    if not isinstance(v, list) or not all(isinstance(t, str) and t for t in v):
        raise DirectiveError(f"expected a list of non-empty strings, got {v!r}")
    return tuple(v)


def _harm_line(v: Any) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v <= 0.5:
        raise DirectiveError(f"cohort_harm_line must be in (0, 0.5], got {v!r}")
    return float(v)


VALIDATORS: dict[str, Callable[[Any], Any]] = {
    "customer_facing_auto_apply": _enum(CUSTOMER_FACING_AUTO),
    "notice_minutes": _int_ge(0),
    "notice_scope": _enum(NOTICE_SCOPES),
    "notice_inside_approver_hours": _bool,
    "max_requests_per_day": _int_ge(1, nullable=True),
    "not_before": _hhmm,
    "avoid_prompt_terms": _terms,
    "weekly_digest": _bool,
    "pre_open_submits": _enum(PRE_OPEN),
    "cohort_harm_line": _harm_line,
}
REQUIRED_DEFAULT_KEYS = frozenset(VALIDATORS) - {"cohort_harm_line"}
SELECTORS = ("use_case_contains", "workflow_id")


@dataclass(frozen=True)
class DirectiveRule:
    """One scoped directive. `values` are validated and parsed (not_before in minutes)."""

    tenant: str
    selector_kind: str
    """all | use_case | workflow"""
    selector_value: str | None
    source: str
    values: FrozenDict
    raw: FrozenDict
    """values as written in the YAML (for the audit table)"""
    order: int

    @property
    def specificity(self) -> tuple[int, int, int]:
        rank = {"all": 0, "use_case": 1, "workflow": 2}[self.selector_kind]
        return (rank, 0 if self.tenant == "*" else 1, self.order)

    @property
    def scope_label(self) -> str:
        if self.selector_kind == "all":
            return self.tenant
        if self.selector_kind == "use_case":
            return f"{self.tenant}/use_case~{self.selector_value}"
        return f"{self.tenant}/workflow={self.selector_value}"

    def matches(self, tenant_id: str, workflow_id: str, use_case: str) -> bool:
        if self.tenant not in ("*", tenant_id):
            return False
        if self.selector_kind == "use_case":
            return (self.selector_value or "").lower() in (use_case or "").lower()
        if self.selector_kind == "workflow":
            return self.selector_value == workflow_id
        return True


class DirectiveLayer:
    """All rules of directives.yaml; `resolve()` gives one workflow's directive."""

    def __init__(self, defaults: DirectiveRule, rules: tuple[DirectiveRule, ...],
                 harm_line_default: float, slot_offset_max_min: int, slot_window_frac: float):
        self.defaults = defaults
        self.rules = rules
        self.harm_line_default = harm_line_default
        self.slot_offset_max_min = slot_offset_max_min
        self.slot_window_frac = slot_window_frac

    def resolve(self, tenant_id: str, workflow_id: str, use_case: str) -> ResolvedDirective:
        matched = sorted((r for r in self.rules if r.matches(tenant_id, workflow_id, use_case)),
                         key=lambda r: r.specificity)
        values: dict[str, Any] = {"cohort_harm_line": self.harm_line_default}
        sources: dict[str, str] = {"cohort_harm_line": "settings.cohort_harm_line_default"}
        for rule in [self.defaults, *matched]:
            for k, v in rule.values.items():
                values[k] = v
                sources[k] = f"{rule.scope_label}: {rule.source}"
        return ResolvedDirective(
            tenant_id=tenant_id, workflow_id=workflow_id,
            customer_facing_auto_apply=values["customer_facing_auto_apply"],
            cohort_harm_line=values["cohort_harm_line"],
            notice_minutes=values["notice_minutes"], notice_scope=values["notice_scope"],
            notice_inside_approver_hours=values["notice_inside_approver_hours"],
            max_requests_per_day=values["max_requests_per_day"],
            not_before_min=values["not_before"],
            avoid_prompt_terms=tuple(values["avoid_prompt_terms"]),
            weekly_digest=values["weekly_digest"],
            preferred_slot_offset_max_min=self.slot_offset_max_min,
            preferred_slot_offset_window_frac=self.slot_window_frac,
            pre_open_submits=values["pre_open_submits"],
            scopes=tuple(r.scope_label for r in [self.defaults, *matched]),
            sources=FrozenDict(sources))

    def unmatched(self, workflows: Iterable[tuple[str, str, str]]) -> list[DirectiveRule]:
        """The rules that match none of the (tenant_id, workflow_id, use_case) workflows: a rule
        like this silently falls back to the defaults, so its memo promise is not in force."""
        wfs = list(workflows)
        return [r for r in self.rules if not any(r.matches(*w) for w in wfs)]

    def rows(self) -> list[tuple[str, str, Any, str]]:
        """(tenant_scope, key, value, source) rows for the `directive` table (as written)."""
        out = []
        for rule in [self.defaults, *self.rules]:
            for k, v in rule.raw.items():
                out.append((rule.scope_label, k, thaw(v), rule.source))
        return out


def _parse_rule(d: Mapping[str, Any], order: int, is_default: bool) -> DirectiveRule:
    allowed = {"source", "set"} if is_default else {"tenant", "selector", "source", "set"}
    extra = set(d) - allowed
    if extra:
        raise DirectiveError(f"unknown keys {sorted(extra)} in directive #{order}")
    raw_set = d.get("set") or {}
    if not isinstance(raw_set, Mapping):
        raise DirectiveError(f"directive #{order}: 'set' must be a mapping")
    unknown = set(raw_set) - set(VALIDATORS)
    if unknown:
        raise DirectiveError(f"directive #{order}: unknown directive keys {sorted(unknown)}")
    if is_default:
        missing = REQUIRED_DEFAULT_KEYS - set(raw_set)
        if missing:
            raise DirectiveError(f"defaults must set {sorted(missing)}")
    values = {k: VALIDATORS[k](v) for k, v in raw_set.items()}
    tenant, kind, value = "*", "all", None
    if not is_default:
        tenant = d.get("tenant")
        if not isinstance(tenant, str) or not tenant:
            raise DirectiveError(f"directive #{order}: 'tenant' is required ('*' for any)")
        sel = d.get("selector") or {}
        if set(sel) - set(SELECTORS) or len(sel) > 1:
            raise DirectiveError(f"directive #{order}: selector is {{}} or one of {SELECTORS}")
        if "use_case_contains" in sel:
            kind, value = "use_case", str(sel["use_case_contains"])
        elif "workflow_id" in sel:
            kind, value = "workflow", str(sel["workflow_id"])
    source = d.get("source")
    if not isinstance(source, str) or not source:
        raise DirectiveError(f"directive #{order}: 'source' (the memo item) is required")
    return DirectiveRule(tenant=tenant, selector_kind=kind, selector_value=value, source=source,
                         values=freeze(values), raw=freeze(raw_set), order=order)


def load_directives(path: str | Path = DEFAULT_PATH,
                    thresholds: Thresholds | None = None) -> DirectiveLayer:
    """Parse and validate directives.yaml. Raises DirectiveError on any malformed entry."""
    t = thresholds or Thresholds()
    doc = yaml.safe_load(Path(path).read_text()) or {}
    extra = set(doc) - {"version", "defaults", "directives"}
    if extra:
        raise DirectiveError(f"unknown top-level keys {sorted(extra)}")
    if doc.get("version") != 1:
        raise DirectiveError("directives.yaml version must be 1")
    if not isinstance(doc.get("defaults"), Mapping):
        raise DirectiveError("directives.yaml needs a 'defaults' block")
    defaults = _parse_rule(doc["defaults"], 0, is_default=True)
    rules = tuple(_parse_rule(d, i + 1, is_default=False)
                  for i, d in enumerate(doc.get("directives") or []))
    seen: set[tuple[str, str]] = {("*", k) for k in defaults.raw}
    for r in rules:
        if r.tenant == "*" and r.selector_kind == "all":
            raise DirectiveError("use 'defaults' for tenant '*' without a selector")
        for k in r.raw:
            if (r.scope_label, k) in seen:
                raise DirectiveError(f"{r.scope_label} sets {k!r} twice")
            seen.add((r.scope_label, k))
    return DirectiveLayer(defaults, rules, harm_line_default=t.cohort_harm_line_default,
                          slot_offset_max_min=t.slot_offset_max_min,
                          slot_window_frac=t.slot_offset_window_frac)
