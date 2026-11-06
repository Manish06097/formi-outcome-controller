-- scope: run
-- Rollups, written in the same transaction as facts; rebuildable from call_fact for post-D0
-- data (DESIGN.md 3.2). Cell-keyed rollups need a known cell: a call whose lead was never seen
-- (cell NULL) is kept in call_fact, counted in loop_step.orphan_calls and left out of r_engage
-- and r_pickup. Counts are additive (ON CONFLICT DO UPDATE SET n = n + excluded.n).

CREATE TABLE r_engage (                             -- connected calls by arm, for the effect model (5.3)
    wf             smallint NOT NULL,
    seg            smallint NOT NULL,
    cell           smallint NOT NULL,
    config_version int NOT NULL,
    prompt         smallint NOT NULL,
    w              smallint NOT NULL,
    v              smallint NOT NULL,
    connected      int NOT NULL DEFAULT 0,
    engaged        int NOT NULL DEFAULT 0,
    flags          int NOT NULL DEFAULT 0,
    PRIMARY KEY (wf, seg, cell, config_version, prompt, w, v)
);

CREATE TABLE r_pickup (                             -- dials by hour and attempt bucket (5.8, 7.5)
    wf             smallint NOT NULL,
    sim_day        smallint NOT NULL,
    hour           smallint NOT NULL,
    cell           smallint NOT NULL,
    attempt_bucket smallint NOT NULL CHECK (attempt_bucket IN (1, 2, 3)),  -- 1 = attempt 1, 2 = attempts 2-3, 3 = 4+
    dials          int NOT NULL DEFAULT 0,
    connected      int NOT NULL DEFAULT 0,
    PRIMARY KEY (wf, sim_day, hour, cell, attempt_bucket)
);

CREATE TABLE r_did (                                -- per-DID pickup keyed by tick (5.9, 7.3)
    wf          smallint NOT NULL,
    tick_no     int NOT NULL,                       -- minute / tick: trailing 1/2/4/8 h windows exact at any stop
    hour        smallint NOT NULL,
    cohort_code smallint NOT NULL,
    did         smallint NOT NULL,
    dials       int NOT NULL DEFAULT 0,
    connected   int NOT NULL DEFAULT 0,
    PRIMARY KEY (wf, tick_no, hour, cohort_code, did)
);

CREATE TABLE r_flags (                              -- hallucination flags per sim hour and prompt (7.1)
    wf        smallint NOT NULL,
    sim_hour  int NOT NULL,
    prompt    smallint NOT NULL,
    connected int NOT NULL DEFAULT 0,
    flags     int NOT NULL DEFAULT 0,
    engaged   int NOT NULL DEFAULT 0,
    PRIMARY KEY (wf, sim_hour, prompt)
);

-- autovacuum scale factor 0.05 on rollups (3.3)
ALTER TABLE r_engage SET (autovacuum_vacuum_scale_factor = 0.05, autovacuum_analyze_scale_factor = 0.05);
ALTER TABLE r_pickup SET (autovacuum_vacuum_scale_factor = 0.05, autovacuum_analyze_scale_factor = 0.05);
ALTER TABLE r_did    SET (autovacuum_vacuum_scale_factor = 0.05, autovacuum_analyze_scale_factor = 0.05);
ALTER TABLE r_flags  SET (autovacuum_vacuum_scale_factor = 0.05, autovacuum_analyze_scale_factor = 0.05);
