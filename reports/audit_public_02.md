# Audit queries on a finished public_02 run

Schema `run_4bdc1ff90f7d_3`. Commands run with PG_DSN set to the private Postgres and CTL_SCHEMA set to the schema.

## why-changed --workflow ks_cancel --at "D3 14:00"
```
ks_cancel at D3 14:00: version 8, applied D3 13:15 by controller_approved; changed prompt_version.
  Proposal 4bdc1f-ks_cancel-D3T1215-0-0006 (incident, route approval, now verified), submitted D3 12:15 on base version 7: Prompt: switch from P2 to P1 (validation run val_00003). Note: this changes the prompt your supervisor set on D3 12:15
  Why: Class P0 (P0a), base version 7. The prompt: incidents.manager, rule -, decision #220 (harm_gate_exempt=P0 prompt fix (0.1), incident_id=ks_cancel-prompt-5055-P2, summary=Change script from P2 to P1 after the D3 12:15 incident: P2 validation 18 failures in 300 (our limit 9 in 300); P1: 3 in 300 (last known good)., tier=lkg, validation_run_id=val_00003). Harm gate passed: P(de < -1%) = 0.0000, P(dd < -2%) = 0.0000, alpha 0.1.
  Impact score 0.0; affected leads 5144.
  Approval apr_00011: requested D3 12:15, decided D3 12:55, status approved. Approver: not available (the API never names the approver).
  Decision 220 at D3 12:15: incidents.manager / patch (rule 7.1/prompt_fix); settings ee07067f8551aa75, code 06666be.
  Verification: {'kind': 'prompt', 'status': 'verified', 'reasons': ['incident ks_cancel-prompt-5055-P2 resolved']}
  What happened after:
    D3 12:15  controller   pending              created: apr_00011
    D3 12:15  controller   submit_approval      governance.lifecycle / submit_approval (rule 6.1/submit) named this proposal
    D3 12:15  controller   release              governance.release / release (rule 6.2/submitted) named this proposal: pending
    D3 13:15  client approver approved             approved: apr_00011
    D3 13:15  controller   applied              applied: version 8
    D3 13:15  controller   apply                governance.lifecycle / apply (rule 6.1/apply) named this proposal
    D3 13:30  controller   config_refreshed     ks_cancel v8 by controller_approved (ours body): prompt_version
    D4 08:00  controller   verified             verified: incident ks_cancel-prompt-5055-P2 resolved
```

## why-changed --workflow ks_cancel --at "D3 11:30"
```
ks_cancel at D3 11:30: version 7, applied D3 11:30 by supervisor; changed voice, prompt_version.
  A client supervisor edit (no proposal of ours). Our reaction is below.
  What happened after:
    D3 12:15  controller   config_refreshed     ks_cancel v7 by supervisor (fetched body): voice, prompt_version
    D3 12:15  controller   rebased              rebased on supervisor v7
    D3 12:15  controller   pending              created: apr_00011
    D3 12:15  controller   rebase               planner.overlay / rebase (rule 6.7/rebase_on_supervisor_edit) in reaction
    D3 12:15  controller   incident_open        incidents.manager / incident_open (rule 7.1) in reaction
    D3 12:15  controller   SUPERVISOR_EDIT      supervisor edited voice, prompt_version of ks_cancel (v7); our work was rebased on it, 0 proposal(s) marked stale
    D3 12:15  controller   EXPERIMENT_INTERRUPTED supervisor v7 edited voice of ks_cancel: experiment ks_cancel:voice:v1 interrupted (decision #219); the next plan starts a new one with their choice as the control
```

## why-changed --workflow nw_renewal --at "D5 15:00"
```
nw_renewal at D5 15:00: version 14, applied D5 15:00 by supervisor; changed voice, caller_id.
  A client supervisor edit (no proposal of ours). Our reaction is below.
  What happened after:
    D5 16:00  controller   config_refreshed     nw_renewal v14 by supervisor (fetched body): voice, caller_id
    D5 16:00  controller   rebased              rebased on supervisor v14
    D5 16:00  controller   rebase               planner.overlay / rebase (rule 6.7/rebase_on_supervisor_edit) in reaction
    D5 16:00  controller   SUPERVISOR_EDIT      supervisor edited voice, caller_id of nw_renewal (v14); our work was rebased on it, 0 proposal(s) marked stale
    D5 16:00  controller   EXPERIMENT_INTERRUPTED supervisor v14 edited voice of nw_renewal: experiment nw_renewal:voice:v1 interrupted (decision #462); the next plan starts a new one with their choice as the control
```

## why-changed --workflow nw_renewal --at "D2 12:00"
```
nw_renewal at D2 12:00: version 8, applied D2 11:00 by controller_auto; changed caller_id.
  Proposal 4bdc1f-nw_renewal-D2T1100-3-0007 (routine, route auto, now verified), submitted - on base version 7: Caller ID: raise D4 from 2.8% to 24.3%; lower D3 from 38.8% to 25.4%; lower D1 from 31.8% to 25.2%; lower D2 from 26.6% to 25%
  Why: Class P3, base version 7. The caller id: learning.did, rule -, decision #141 (kind=caller_id, scope=default, tv=0.2155). Harm gate FAILED: P(de < -1%) = 0.0000, P(dd < -2%) = 0.0635, alpha 0.025; failed ['did'].
  Impact score 0.0; affected leads 3672.
  Auto-applied under the workflow's auto-apply policy (no approval).
  Decision 141 at D2 11:00: learning.did / patch (rule 5.9/plan_caller_ids); settings ee07067f8551aa75, code 06666be.
  Verification: {'kind': 'caller_id', 'status': 'verified', 'reasons': ['caller_id ok: {"did_dials_after": 0, "locked_campaigns_allowed": false, "hours": 2.0, "ratio": 1.1866666666666659, "ucb": 1.3422543103503455, "dials": 600}']}
  What happened after:
    D2 11:00  controller   applied              applied: version 8
    D2 11:00  controller   auto_apply           governance.lifecycle / auto_apply (rule 6.5/auto) named this proposal
    D2 11:15  controller   config_refreshed     nw_renewal v8 by controller_auto (ours body): caller_id
    D2 13:45  controller   verified             verified: caller_id ok: {"did_dials_after": 0, "locked_campaigns_allowed": false, "hours": 2.0, "ratio": 1.1866666666666659, "ucb": 1.3422543103503455, "dials": 600}
```

## why-heard --lead ks_cancel:l00024687 --day 3
```
Lead ks_cancel:l00024687 (person ks_cancel:p0017502, ks_cancel; lang kn, city_tier 2, age_band 31-45, value_band mid) on D3: 1 call(s).
  D3 12:09 attempt 2, campaign ks_cancel:c0017, cohort age1830_3145_4660_60p_kn~1fabbc, dialled with version 7 (live 7): connected, flagged.
    welcome_message W1 at 59.6%: set by the client supervisor; dialled with version 7, randomised per call.
    voice V4 at 14.7%: set by the client supervisor; dialled with version 7, randomised per call.
    caller_id D1 at 50%: set by the client supervisor (inherited from default: cohort age1830_3145_4660_60p_kn~1fabbc has no override); dialled with version 7, randomised per call.
    prompt_version P2 at 100%: set by the client supervisor; the prompt live at the call minute (version 7), one per workflow, applied immediately even on locked campaigns.
```
