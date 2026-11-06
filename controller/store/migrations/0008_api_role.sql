-- scope: run
-- Role ctl_api: SELECT on this run schema, INSERT and UPDATE on hold only (DESIGN.md 2, 9.3).
-- Skipped with a notice when the role does not exist (no CREATEROLE privilege, see 0001).
DO $$
DECLARE
    sch text := current_schema();
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ctl_api') THEN
        RAISE NOTICE 'role ctl_api missing: grants skipped for %', sch;
        RETURN;
    END IF;
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO ctl_api', sch);
    EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO ctl_api', sch);
    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT ON TABLES TO ctl_api', sch);
    EXECUTE format('GRANT INSERT, UPDATE ON %I.hold TO ctl_api', sch);
    EXECUTE format('GRANT USAGE ON SEQUENCE %s TO ctl_api',
                   pg_get_serial_sequence(format('%I.hold', sch), 'hold_id'));
END $$;
