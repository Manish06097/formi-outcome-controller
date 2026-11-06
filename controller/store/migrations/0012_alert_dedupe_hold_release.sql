-- scope: run
-- Integration step I1 (ADR 0028, T5 requests).
-- 1. Alerts are de-duplicated on (code, workflow, dedupe_key) by a unique index over open rows,
--    instead of a "[subject] " prefix inside `message`. Old prefixed rows are split; an older
--    open duplicate (impossible under either old writer, but cheap to rule out) is closed first.
ALTER TABLE alert ADD COLUMN dedupe_key text NOT NULL DEFAULT '';
UPDATE alert SET dedupe_key = substring(message FROM '^\[([^]]*)\] '),
                 message = regexp_replace(message, '^\[[^]]*\] ', '')
WHERE message ~ '^\[[^]]*\] ';
UPDATE alert a SET closed_wall = now()
WHERE a.closed_wall IS NULL AND EXISTS (
    SELECT 1 FROM alert b WHERE b.closed_wall IS NULL AND b.code = a.code
      AND coalesce(b.wf, -1) = coalesce(a.wf, -1) AND b.dedupe_key = a.dedupe_key
      AND b.alert_id > a.alert_id);
CREATE UNIQUE INDEX alert_open_key ON alert (code, coalesce(wf, -1), dedupe_key)
    WHERE closed_wall IS NULL;
-- 2. Who released a supervisor hold is a column, not a suffix appended to `reason`. ctl_api's
--    UPDATE grant on hold (0008) covers the new column.
ALTER TABLE hold ADD COLUMN released_by text;
UPDATE hold SET released_by = substring(reason FROM ' \[released by (.*) at [^]]*\]$'),
                reason = regexp_replace(reason, ' \[released by .* at [^]]*\]$', '')
WHERE reason ~ ' \[released by .* at [^]]*\]$';
