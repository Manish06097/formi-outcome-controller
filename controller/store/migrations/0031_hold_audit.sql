-- scope: run
-- Track R (AUDIT-1 finding 15): a supervisor hold changes decisions, so it belongs in audit_log.
-- A trigger writes the row in the hold's own transaction, whoever wrote the hold and whether or not
-- the controller is running. It runs as the function's owner, so role ctl_api keeps its narrow
-- grant (INSERT and UPDATE on hold only, migration 0008) and cannot write audit_log directly.
CREATE FUNCTION hold_audit() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path FROM CURRENT
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO audit_log (sim_minute, wf, actor, action, subject_type, subject_id, summary, details)
        VALUES (NEW.from_minute, NEW.wf, NEW.set_by, 'hold_set', 'hold', NEW.hold_id::text,
                format('hold set by %s: %s', NEW.set_by, NEW.reason),
                jsonb_build_object('until_minute', NEW.until_minute, 'reason', NEW.reason));
    ELSIF NEW.released_minute IS NOT NULL AND OLD.released_minute IS NULL THEN
        INSERT INTO audit_log (sim_minute, wf, actor, action, subject_type, subject_id, summary, details)
        VALUES (NEW.released_minute, NEW.wf, coalesce(NEW.released_by, 'unknown'), 'hold_released',
                'hold', NEW.hold_id::text,
                format('hold released by %s', coalesce(NEW.released_by, 'unknown')),
                jsonb_build_object('from_minute', NEW.from_minute));
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER hold_audit_row AFTER INSERT OR UPDATE ON hold
    FOR EACH ROW EXECUTE FUNCTION hold_audit();
