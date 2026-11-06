-- scope: run
-- Integration step I1 (ADR 0028, T1 request): REFRESH marks the config_changed events it handled
-- on the event row, instead of looking for a `config_refreshed` audit row. Events a pre-0011
-- controller already refreshed are backfilled from those audit rows, so a resumed run does not
-- handle them twice.
ALTER TABLE config_event ADD COLUMN refreshed_minute int;
UPDATE config_event e SET refreshed_minute = a.sim_minute
FROM workflow w, audit_log a
WHERE w.wf = e.wf AND a.action = 'config_refreshed' AND a.subject_type = 'config_version'
  AND a.subject_id = w.workflow_id || ':' || e.version;
CREATE INDEX config_event_unrefreshed ON config_event (event_id) WHERE refreshed_minute IS NULL;
