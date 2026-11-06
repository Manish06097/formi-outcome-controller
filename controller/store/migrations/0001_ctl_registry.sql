-- scope: ctl
-- Control schema shared by every run (DESIGN.md 2, 8.5). Applied with search_path = ctl.
-- Owner: M0. Later contract changes add new numbered files; applied files are never edited
-- (the runner refuses a checksum mismatch).

CREATE TABLE run_registry (
    schema_name     text PRIMARY KEY,               -- run_<fp12>_<n>
    fingerprint     text NOT NULL,                  -- sha256 of the world identity (8.5)
    sim_url         text NOT NULL,                  -- normalised SIM_URL
    created_wall    timestamptz NOT NULL DEFAULT now(),
    status          text NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'done', 'abandoned')),
    last_sim_minute int
);
CREATE INDEX run_registry_fingerprint ON run_registry (fingerprint, created_wall DESC);

-- The read-only API role (DESIGN.md 2, 9.3). Cluster-wide, so created here once. A controller
-- connected without CREATEROLE skips it with a notice; the API then cannot start, nothing else
-- breaks. The API deployment (M7) sets LOGIN and the password.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ctl_api') THEN
        BEGIN
            CREATE ROLE ctl_api NOLOGIN;
        EXCEPTION
            WHEN duplicate_object THEN NULL;
            WHEN insufficient_privilege THEN
                RAISE NOTICE 'no CREATEROLE privilege: role ctl_api not created';
        END;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ctl_api') THEN
        GRANT USAGE ON SCHEMA ctl TO ctl_api;
        GRANT SELECT ON ctl.run_registry TO ctl_api;
    END IF;
END $$;
