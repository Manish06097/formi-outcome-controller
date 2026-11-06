-- scope: run
-- Integration step I2 (ADR 0029): what we send must survive the store byte for byte. jsonb
-- re-orders object keys (shorter keys first), and the simulator keeps a body's key order: the
-- order of a distribution's options decides which option each dial draws, so a body rebuilt
-- from jsonb after a restart (a resent request, or a composition from restored patches) would
-- change every later dial of the world. `json` keeps the exact text.
ALTER TABLE proposal ALTER COLUMN request TYPE json USING request::json;
ALTER TABLE patch ALTER COLUMN value TYPE json USING value::json;
ALTER TABLE patch ALTER COLUMN evidence TYPE json USING evidence::json;
