-- scope: run
-- Integration step I1 (ADR 0028, T2 request): the P0 order of a proposal is a column, not a key
-- inside `evidence`. Release precedence and supersede (ADR 0013) read it; NULL unless class 0.
ALTER TABLE proposal ADD COLUMN p0_rank smallint CHECK (p0_rank IN (0, 1));
ALTER TABLE proposal ADD CONSTRAINT proposal_p0_rank_class
    CHECK ((class = 0) = (p0_rank IS NOT NULL));
