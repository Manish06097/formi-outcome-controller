-- scope: run
-- Catalog and identity (DESIGN.md 3.2). Applied inside one run schema run_<fp12>_<n> with
-- search_path set to it, so every name below is unqualified and lands in that schema.
-- wf is a dense smallint workflow code; option codes are smallints indexing the workflow's
-- option lists from the API (WorkflowSpec.options[dim]).

CREATE TABLE workflow (
    wf             smallint PRIMARY KEY,
    workflow_id    text NOT NULL UNIQUE,
    tenant_id      text NOT NULL,
    spec           jsonb NOT NULL,                  -- GET /workflows/{id}
    spec_hash      text NOT NULL,
    fetched_minute int
);

CREATE TABLE directive (                            -- directives.yaml, loaded at start for audit (6.5)
    tenant_scope text NOT NULL,                     -- '*', '<tenant>', '<tenant>/use_case~<kw>', '<tenant>/workflow=<id>'
    key          text NOT NULL,
    value        jsonb NOT NULL,
    source       text NOT NULL,
    PRIMARY KEY (tenant_scope, key)
);

CREATE TABLE cell (                                 -- observed (lang, tier, age, value) combinations (5.2)
    cell_id smallint PRIMARY KEY,
    lang    text NOT NULL,
    tier    text NOT NULL,
    age     text NOT NULL,
    value   text NOT NULL,
    UNIQUE (lang, tier, age, value)
);

CREATE TABLE cohort_def (                           -- versioned cohort identity (5.2)
    cohort_key     text PRIMARY KEY,                -- <slug>~<sha1[:6]>, also the simulator cohort id
    wf             smallint NOT NULL REFERENCES workflow (wf),
    where_canon    jsonb NOT NULL,
    def_hash       text NOT NULL,
    cells          smallint[] NOT NULL DEFAULT '{}',
    origin         text NOT NULL CHECK (origin IN ('grid', 'business', 'supervisor', 'segment')),
    created_minute int
);

CREATE TABLE config_version (                       -- every simulator version, with its body if known
    wf             smallint NOT NULL REFERENCES workflow (wf),
    version        int NOT NULL,
    actor          text NOT NULL,                   -- business_default | supervisor | controller_approved | controller_auto
    config         jsonb,                           -- Config.stored(); NULL when body_source = unknown
    config_hash    text,                            -- Config.hash()
    applied_minute int,                             -- NULL for the business default (version 1)
    proposal_id    text,
    approval_id    text,
    changed_dims   text[] NOT NULL DEFAULT '{}',
    body_source    text NOT NULL CHECK (body_source IN ('ours', 'fetched', 'unknown')),
    PRIMARY KEY (wf, version)
);
CREATE INDEX config_version_applied ON config_version (wf, applied_minute);

CREATE TABLE config_cohort (                        -- first-match resolution per version (5.2, 9.4)
    wf              smallint NOT NULL,
    version         int NOT NULL,
    position        smallint NOT NULL,              -- order in the cohort list; default last
    cohort_id       text NOT NULL,                  -- as in the config ('default' included)
    cohort_key      text,                           -- our cohort_def key; NULL for 'default'
    cohort_code     smallint NOT NULL,              -- the code call_fact.cohort_code uses; 0 = default
    owner           text NOT NULL CHECK (owner IN ('ours', 'supervisor')),
    effective_cells smallint[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (wf, version, cohort_id),
    UNIQUE (wf, version, cohort_code),
    FOREIGN KEY (wf, version) REFERENCES config_version (wf, version)
);
