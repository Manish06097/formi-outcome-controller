-- scope: run
-- Integration step I1 (ADR 0028, T4 request): the flag CUSUM's unit of progress is the sim hour
-- (`r_flags` is hourly), so the column says so instead of reusing `last_event_id`.
ALTER TABLE flag_monitor RENAME COLUMN last_event_id TO last_sim_hour;
ALTER TABLE flag_monitor ALTER COLUMN last_sim_hour TYPE int;
