# 0023 Scope cuts

Status: accepted (lead, 2026-11-07).

## Context
The brief grades scope and stakeholder judgement (20%), not coverage. The six stakeholder notes ask
for more than one build can do well. A cut is only defensible if it is written down with the reason
and the cost to finish.

## Decision
Cut, in this order of how little they change the outcome:
1. **Per-person call time** (stakeholder note on call timing). 2 to 3 dials per person per campaign
   is too little data per person, and a wrong guess costs a large factor in every other hour. Built
   instead: per-cohort hour weights through the schedule lever.
2. **Prompt A/B outside incidents.** One prompt per workflow means no concurrent control, so an
   effect of a few percent needs on the order of 14,000 connected calls per arm. Prompts change
   only through the incident path.
3. **Cross-workflow promotion of winners.** The product notes call it a later problem.
4. **Interaction likelihood-ratio test and regrid** (welcome x voice). Each refines a built
   capability. The additive model is stated as an assumption and the failure mode is in DESIGN 13.
5. **SNIPS replay for impact.** High variance at dev volume; impact is the stratified posterior
   replay already built.
6. **HTML supervisor page.** The JSON API per tenant and the holds endpoint are built; the page,
   auth and email digest are not.
7. **Policy fuzzing beyond the named attack cases.** The local policy replica is covered by a
   differential test (2,100 bodies, 0 disagreements) and by the attack cases.

Kept minimal rather than cut: the supervisor view (C11), impact and dry run (C10), the shared
cohort object (C2).

## Alternatives
- Build per-person timing and skip the incident safety work: rejected, an incident harms real
  leads and is graded on the hidden scenarios; call time is a modest, noisy gain.
- Build the HTML page: rejected, the supervisor needs the information and a decision path, which
  the JSON view and holds endpoint give; the page adds no decision.

## Consequences
- What it would take to finish each item is in the capability table of `SUBMISSION.md` (section on
  scope), written so a reviewer can change a constraint and see which cut moves.
- The hours spent are not recorded here: the candidate states them in `SUBMISSION.md`, honestly.
