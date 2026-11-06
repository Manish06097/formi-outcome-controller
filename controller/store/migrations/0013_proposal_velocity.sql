-- scope: run
-- Integration step I2 (ADR 0029): the velocity ledger of DESIGN.md 6.5 rule 4 is derived from
-- the proposals themselves, so it is never lost and never double-counted after a crash.
--   velocity        [[cohort, dim, tv, increasing], ...] of the composition, written with the
--                   draft (before any side effect), counted only once the proposal is applied
--   applied_minute  the sim minute the proposal's config became live (auto-apply or apply)
ALTER TABLE proposal ADD COLUMN velocity jsonb;
ALTER TABLE proposal ADD COLUMN applied_minute int;
CREATE INDEX proposal_auto_applied ON proposal (wf, applied_minute)
    WHERE route = 'auto' AND applied_minute IS NOT NULL;
