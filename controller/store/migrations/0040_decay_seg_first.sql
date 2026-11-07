-- scope: run
-- ADR 0034: the first segment a decay monitor folded, so "two clean segments" is countable.
ALTER TABLE decay_monitor ADD COLUMN seg_first smallint;
