-- scope: run
-- Track R (ADR 0019, loop/safemode.py). Safe mode's memory, kept here so restarts share it.
-- Numbered 0030 to stay clear of migrations other tracks add in parallel.
-- hook_attempt holds only what is in flight: a row is deleted as soon as the hook returns, so a row
-- that survives a restart is a crash. started - raised counts deaths with no exception.
CREATE TABLE hook_attempt (
    point      text NOT NULL,
    step_no    int  NOT NULL,
    started    int  NOT NULL DEFAULT 0,
    raised     int  NOT NULL DEFAULT 0,
    last_error text,
    PRIMARY KEY (point, step_no)
);
-- A hook with a row here is in safe mode for the rest of this run.
CREATE TABLE hook_guard (
    point            text PRIMARY KEY,
    state            text NOT NULL CHECK (state IN ('guarded', 'disabled')),
    since_step       int  NOT NULL,
    since_minute     int  NOT NULL,
    reason           text NOT NULL,
    skips            int  NOT NULL DEFAULT 0,
    last_skip_minute int
);
