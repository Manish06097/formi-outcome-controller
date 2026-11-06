# 0012. Directives layer

Status: accepted (2026-11-06), milestone M3 (track T2; the loader is M0's, ADR 0027 section 5)

## Context
- The memos contain client agreements the simulator does not know: Northwind Compliance wants a
  named human approval for anything customer-facing and is strict about cohorts "even briefly";
  Kestrel's cancellation supervisor wants two requests a day, never before 11:00, and to be told
  before anything changes on a running campaign.
- public_03 `nort_cancel_2` lets welcome and voice auto-apply, which contradicts the Northwind
  memo (F15).
- Hidden scenarios bring tenants with no memo at all.

## Decision
- Directives live in `controller/directives.yaml`, scoped by `(tenant, selector)` with selectors
  `{}` (tenant-wide), `use_case_contains` and `workflow_id`, resolved key by key from `*` defaults
  up to the most specific scope (M0 loader).
- `world/registry.effective_policy` combines a workflow's API policy with its resolved directive,
  **taking the stricter value of each field**: `C_eff = min(C_policy, cap)`; `allowed_dims` loses
  the customer-facing dims when the directive says `none`; `cohort_harm_line = min(default,
  directive)`; a directive cannot enable auto-apply or widen `max_shift`. API-only fields come
  straight from `GET /workflows/{id}`.
- `customer_facing_auto` is enforced in `policy.check_auto_apply` per changed (cohort, dim):
  `none` refuses, `risk_reducing_only` (the `*` default) allows only risk-reducing changes,
  `ramps_and_risk_reducing` (Kestrel cancel) also allows ramps, meaning share moves between options
  already live with no option entering.
- Notice: `notice_minutes` before an auto-apply in `notice_scope` (`customer_facing` for `*`,
  `non_incident` for Kestrel), never for P0. With `notice_inside_approver_hours` the notice is
  posted at the next approver opening when the work is ready outside hours.
- `WorkflowRegistry.auto_apply_conflict(wf)` reports dims the API allows but the directive forbids,
  with the memo source, for the supervisor view and the Compliance reply (F15).
- `WorkflowRegistry.load()` returns every effective-policy field that changed since the previous
  read (9.7 "change a constraint").

## Alternatives
- *Trust the API policy only.* Breaks the Northwind agreement on nort_cancel_2.
- *Apply Kestrel's ops asks tenant-wide.* Her memo speaks for the cancellation line only (A-11);
  Kestrel renewal and KYC get the `*` default.
- *Default for unknown tenants = no customer-facing auto-apply at all.* Arm removals and rollbacks
  would then wait for a routine slot while customers keep hearing a losing variant; moving share
  back to the incumbent reduces exposure, so `risk_reducing_only` is the default.

## Consequences
- Ramps on memo-less tenants wait for the routine slot (the harm gate limits each step anyway).
- `EffectivePolicy` lacked `pre_open_submits` and a few other fields, so the registry returned an
  `ExtendedPolicy` subclass. Resolved in step I1: the fields are on `EffectivePolicy` (ADR 0028).
- The Northwind harm line is 1%, which halves the first exploration share (5.4).
