-- scope: run
-- Integration step I5 (ADR 0032): prompt safety in the loop.
-- validation_run gains what the result transaction of a validation intent records (the pool that
-- paid, the cost, the decision the run was designated gate evidence for). prompt_trigger holds a
-- supervisor prompt change seen in REFRESH until DETECT has judged it (REFRESH runs in a
-- transaction and cannot make the HTTP validation call).
ALTER TABLE validation_run
    ADD COLUMN designated   bool NOT NULL DEFAULT false,
    ADD COLUMN decision_key text,
    ADD COLUMN pool         text,
    ADD COLUMN cost         real;

CREATE TABLE prompt_trigger (
    wf         smallint NOT NULL,
    version    int      NOT NULL,
    prompt     smallint NOT NULL,
    actor      text     NOT NULL,
    sim_minute int      NOT NULL,
    handled    bool     NOT NULL DEFAULT false,
    PRIMARY KEY (wf, version)
);
