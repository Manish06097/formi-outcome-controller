-- Set-based transform of one chunk into typed facts and rollups (DESIGN.md 3.1).
--
-- Owner: track T1 (milestone M1). Run by controller/ingest/pipeline.py, statement by statement,
-- inside ONE transaction per chunk together with the ingested_chunk row (exactly once).
-- Each statement starts with a "-- name: <step>" line; the pipeline times every step.
-- "-- paths: sql" marks a step only the SQL path runs (the Python fallback fills ev itself).
--
-- Parameters: chunk_no, kind ('history' | 'events'), epoch0 (epoch seconds of D0 00:00 from
-- GET /sim/state), tick (tick_minutes), until_minute, prev_until_minute (until_minute of the
-- previous ingested chunk, or a very small number), events, first_event_id, last_event_id,
-- bytes, txn_ms.
--
-- Rules:
-- * Order by event_id, never ts (ts is not monotonic inside a chunk).
-- * A dial's hour, segment and tick come from its tick start, floor(minute / tick) * tick: the
--   simulator randomises the call ts inside the tick, and the tick start is the minute whose
--   hour the dialler used.
-- * History calls go into rollups and lead state only; post-D0 calls also into call_fact.
-- * Rollups are additive upserts, so they equal a from-scratch recomputation over call_fact.
-- * Option codes are 0-based indexes into the workflow's option lists from GET /workflows;
--   an unknown option name gets -1 (never expected; it would show in the recomputation check).

-- name: create_ev
CREATE TEMP TABLE ev (
    event_id bigint, type text, minute int, workflow_id text, campaign_id text, lead_id text,
    person_id text, attempt_no int, day_index int, config_version int, cohort_id text,
    w_name text, v_name text, did_name text, prompt_name text, connected bool, engaged bool,
    disposition text, duration_s int, turns int, flag bool, adherence real, lang text,
    tier text, age text, value text, leads int, cv_at_creation int, version int, actor text,
    changed_dims text[], proposal_id text, approval_id text, status text, reasons jsonb,
    body jsonb
) ON COMMIT DROP;

-- name: parse
-- paths: sql
INSERT INTO ev
SELECT r.event_id, r.type,
       floor((extract(epoch FROM r.ts) - %(epoch0)s) / 60)::int,
       r.workflow_id, r.campaign_id, r.lead_id, r.person_id, r.attempt_no,
       r.campaign_day_index, r.config_version, r.cohort_id,
       r.variant ->> 'welcome_message', r.variant ->> 'voice', r.variant ->> 'caller_id',
       r.variant ->> 'prompt_version', r.connected, r.engaged, r.disposition, r.duration_s,
       r.turns, (r.qa ->> 'hallucination_flag')::bool, (r.qa ->> 'flow_adherence')::real,
       r.features ->> 'lang', r.features ->> 'city_tier', r.features ->> 'age_band',
       r.features ->> 'value_band', r.leads, r.config_version_at_creation, r.version, r.actor,
       CASE WHEN jsonb_typeof(r.changed_dims) = 'array'
            THEN ARRAY(SELECT jsonb_array_elements_text(r.changed_dims)) END,
       r.proposal_id, r.approval_id, r.status, r.reasons,
       CASE WHEN r.type IS NULL OR r.type NOT IN ('campaign_created', 'lead', 'call',
                                                  'config_changed', 'approval_decided')
            THEN s.j END
FROM (SELECT line::jsonb AS j FROM stage_raw WHERE chunk_no = %(chunk_no)s) s,
     LATERAL jsonb_to_record(s.j) AS r(
         type text, event_id bigint, ts timestamptz, workflow_id text, campaign_id text,
         lead_id text, person_id text, attempt_no int, campaign_day_index int,
         config_version int, cohort_id text, variant jsonb, connected bool, engaged bool,
         disposition text, duration_s int, turns int, qa jsonb, features jsonb, leads int,
         config_version_at_creation int, version int, actor text, changed_dims jsonb,
         proposal_id text, approval_id text, status text, reasons jsonb);

-- name: check
SELECT count(*), coalesce(min(event_id), 0), coalesce(max(event_id), 0),
       count(*) FILTER (WHERE event_id IS NULL OR minute IS NULL)
FROM ev;

-- name: analyze
ANALYZE ev;

-- name: wfx
CREATE TEMP TABLE wfx ON COMMIT DROP AS
SELECT wf, workflow_id,
       (spec -> 'dialling_window' ->> 0)::int * 60 AS win_lo,
       (spec -> 'dialling_window' ->> 1)::int * 60 AS win_hi,
       jsonb_array_length(spec -> 'cadence') AS cad_len,
       (SELECT sum(x::int) FROM jsonb_array_elements_text(spec -> 'cadence') x) AS cad_sum
FROM workflow;

-- name: opt
CREATE TEMP TABLE opt ON COMMIT DROP AS
SELECT w.wf, d.key AS dim, o.name, (o.ord - 1)::smallint AS code
FROM workflow w, jsonb_each(w.spec -> 'options') d,
     jsonb_array_elements_text(d.value) WITH ORDINALITY o(name, ord);

-- name: config_events
INSERT INTO config_event (event_id, wf, version, actor, changed_dims, proposal_id, approval_id,
                          minute)
SELECT e.event_id, x.wf, e.version, coalesce(e.actor, 'unknown'), coalesce(e.changed_dims, '{}'),
       e.proposal_id, e.approval_id, e.minute
FROM ev e JOIN wfx x USING (workflow_id)
WHERE e.type = 'config_changed'
ORDER BY e.event_id
ON CONFLICT (event_id) DO NOTHING;

-- name: config_versions
-- Every version an event names, with an unknown body; the ConfigMirror fills bodies later.
INSERT INTO config_version (wf, version, actor, config, config_hash, applied_minute, proposal_id,
                            approval_id, changed_dims, body_source)
SELECT DISTINCT ON (x.wf, e.version) x.wf, e.version, coalesce(e.actor, 'unknown'), NULL, NULL,
       e.minute, e.proposal_id, e.approval_id, coalesce(e.changed_dims, '{}'), 'unknown'
FROM ev e JOIN wfx x USING (workflow_id)
WHERE e.type = 'config_changed'
ORDER BY x.wf, e.version, e.event_id
ON CONFLICT (wf, version) DO NOTHING;

-- name: call_versions
-- Versions seen only on calls or campaigns (version 1 is the business default, never an event).
INSERT INTO config_version (wf, version, actor, config, config_hash, applied_minute, proposal_id,
                            approval_id, changed_dims, body_source)
SELECT DISTINCT x.wf, v.version,
       CASE WHEN v.version = 1 THEN 'business_default' ELSE 'unknown' END,
       NULL::jsonb, NULL::text, NULL::int, NULL::text, NULL::text, '{}'::text[], 'unknown'
FROM (SELECT workflow_id, config_version AS version FROM ev WHERE type = 'call'
      UNION SELECT workflow_id, cv_at_creation FROM ev WHERE type = 'campaign_created') v
JOIN wfx x USING (workflow_id)
WHERE v.version IS NOT NULL
ORDER BY 1, 2
ON CONFLICT (wf, version) DO NOTHING;

-- name: cohort_need
CREATE TEMP TABLE IF NOT EXISTS cohort_need (wf smallint, version int, position smallint,
                                             cohort_id text) ON COMMIT DELETE ROWS;

-- name: cohort_need_fill
INSERT INTO cohort_need (wf, version, position, cohort_id)
SELECT DISTINCT x.wf, e.config_version, -1, coalesce(e.cohort_id, 'default')
FROM ev e JOIN wfx x USING (workflow_id)
WHERE e.type = 'call';

-- name: cohort_codes
-- (replaced by controller.store.repos.COHORT_CODES_SQL: one definition for ingest and mirror)
@COHORT_CODES_SQL;

-- name: cells
INSERT INTO cell (cell_id, lang, tier, age, value)
SELECT (SELECT coalesce(max(cell_id), 0) FROM cell)
           + row_number() OVER (ORDER BY n.lang, n.tier, n.age, n.value),
       n.lang, n.tier, n.age, n.value
FROM (SELECT DISTINCT coalesce(e.lang, '') AS lang, coalesce(e.tier, '') AS tier,
                      coalesce(e.age, '') AS age, coalesce(e.value, '') AS value
      FROM ev e WHERE e.type = 'lead') n
WHERE NOT EXISTS (SELECT 1 FROM cell c WHERE c.lang = n.lang AND c.tier = n.tier
                                         AND c.age = n.age AND c.value = n.value);

-- name: campaigns
INSERT INTO campaign (campaign_id, wf, created_minute, sim_day, leads, config_version_at_creation)
SELECT e.campaign_id, x.wf, e.minute, floor(e.minute / 1440.0)::smallint, coalesce(e.leads, 0),
       coalesce(e.cv_at_creation, 0)
FROM ev e JOIN wfx x USING (workflow_id)
WHERE e.type = 'campaign_created'
ORDER BY e.event_id
ON CONFLICT (campaign_id) DO NOTHING;

-- name: leads
-- lead_sk is the lead event's event_id: deterministic, unique and gap-free across crashes.
INSERT INTO lead_dim (lead_sk, lead_id, person_id, wf, campaign_id, cell, created_minute, state,
                      attempts)
SELECT e.event_id, e.lead_id, e.person_id, x.wf, e.campaign_id, c.cell_id, e.minute, 0, 0
FROM ev e JOIN wfx x USING (workflow_id)
LEFT JOIN cell c ON c.lang = coalesce(e.lang, '') AND c.tier = coalesce(e.tier, '')
                AND c.age = coalesce(e.age, '') AND c.value = coalesce(e.value, '')
WHERE e.type = 'lead'
ORDER BY e.event_id
ON CONFLICT (lead_id) DO NOTHING;

-- name: calls
CREATE TEMP TABLE calls ON COMMIT DROP AS
SELECT e.event_id, x.wf, l.lead_sk, l.cell, e.minute, t.tstart, t.sim_day,
       (t.tstart - t.sim_day * 1440) / 60 AS hour,
       2 * t.sim_day + CASE WHEN t.tstart - t.sim_day * 1440 >= (x.win_lo + x.win_hi) / 2
                            THEN 1 ELSE 0 END AS seg,
       floor(t.tstart / 60.0)::int AS sim_hour,
       t.tstart / %(tick)s AS tick_no,
       coalesce(e.attempt_no, 0) AS attempt_no,
       CASE WHEN coalesce(e.attempt_no, 0) <= 1 THEN 1
            WHEN e.attempt_no <= 3 THEN 2 ELSE 3 END AS attempt_bucket,
       coalesce(e.day_index, 0) AS day_index, coalesce(e.config_version, 0) AS config_version,
       coalesce(cc.cohort_code, 0) AS cohort_code,
       coalesce(ow.code, -1) AS w, coalesce(ov.code, -1) AS v, coalesce(od.code, -1) AS did,
       coalesce(op.code, -1) AS prompt,
       coalesce(e.connected, false) AS connected, coalesce(e.engaged, false) AS engaged,
       coalesce(e.disposition = 'concluded_interested', false) AS interested,
       coalesce(e.flag, false) AS flag, e.adherence,
       coalesce(e.duration_s, 0) AS duration_s, coalesce(e.turns, 0) AS turns
FROM ev e
JOIN wfx x USING (workflow_id)
CROSS JOIN LATERAL (
    SELECT floor(e.minute / %(tick)s::numeric)::int * %(tick)s AS tstart,
           floor(floor(e.minute / %(tick)s::numeric) * %(tick)s / 1440.0)::int AS sim_day
) t
LEFT JOIN lead_dim l ON l.lead_id = e.lead_id
LEFT JOIN config_cohort cc ON cc.wf = x.wf AND cc.version = e.config_version
                          AND cc.cohort_id = coalesce(e.cohort_id, 'default')
LEFT JOIN opt ow ON ow.wf = x.wf AND ow.dim = 'welcome_message' AND ow.name = e.w_name
LEFT JOIN opt ov ON ov.wf = x.wf AND ov.dim = 'voice' AND ov.name = e.v_name
LEFT JOIN opt od ON od.wf = x.wf AND od.dim = 'caller_id' AND od.name = e.did_name
LEFT JOIN opt op ON op.wf = x.wf AND op.dim = 'prompt_version' AND op.name = e.prompt_name
WHERE e.type = 'call';

-- name: call_fact
INSERT INTO call_fact (event_id, wf, lead_sk, minute, sim_day, hour, seg, attempt_no, day_index,
                       config_version, cohort_code, cell, w, v, did, prompt, connected, engaged,
                       interested, flag, adherence, duration_s, turns)
SELECT event_id, wf, lead_sk, minute, sim_day, hour, seg, attempt_no, day_index, config_version,
       cohort_code, cell, w, v, did, prompt, connected, engaged, interested, flag, adherence,
       duration_s, turns
FROM calls
WHERE %(kind)s = 'events'
ORDER BY event_id;

-- name: lead_calls
-- attempts and last attempt from the calls; concluded on the first engaged call; exhausted when
-- every attempt of the cadence is used (time-based exhaustion is lead_expiry below).
UPDATE lead_dim l
SET attempts = greatest(l.attempts, a.attempts),
    last_attempt_min = greatest(coalesce(l.last_attempt_min, a.last_min), a.last_min),
    concluded_min = coalesce(l.concluded_min, a.concluded),
    state = CASE WHEN l.state = 1 OR a.concluded IS NOT NULL THEN 1
                 WHEN greatest(l.attempts, a.attempts) >= a.cad_sum THEN 2
                 ELSE l.state END
FROM (SELECT c.lead_sk, max(c.attempt_no) AS attempts, max(c.minute) AS last_min,
             min(c.minute) FILTER (WHERE c.engaged) AS concluded, max(x.cad_sum) AS cad_sum
      FROM calls c JOIN wfx x ON x.wf = c.wf
      WHERE c.lead_sk IS NOT NULL GROUP BY c.lead_sk) a
WHERE l.lead_sk = a.lead_sk;

-- name: lead_expiry
-- A lead whose campaign is older than its cadence can never be dialled again (the simulator
-- marks it unreachable at the first tick of day sim_day + len(cadence)). Applied once per
-- campaign, in the chunk whose until_minute first reaches that day.
UPDATE lead_dim l SET state = 2
FROM campaign c JOIN wfx x ON x.wf = c.wf
WHERE l.campaign_id = c.campaign_id AND l.state = 0
  AND c.sim_day + x.cad_len <= floor(%(until_minute)s / 1440.0)
  AND c.sim_day + x.cad_len > floor(%(prev_until_minute)s / 1440.0);

-- name: r_engage
INSERT INTO r_engage AS r (wf, seg, cell, config_version, prompt, w, v, connected, engaged, flags)
SELECT wf, seg, cell, config_version, prompt, w, v, count(*), count(*) FILTER (WHERE engaged),
       count(*) FILTER (WHERE flag)
FROM calls WHERE connected AND cell IS NOT NULL
GROUP BY wf, seg, cell, config_version, prompt, w, v
ORDER BY wf, seg, cell, config_version, prompt, w, v
ON CONFLICT (wf, seg, cell, config_version, prompt, w, v) DO UPDATE SET
    connected = r.connected + excluded.connected, engaged = r.engaged + excluded.engaged,
    flags = r.flags + excluded.flags;

-- name: r_pickup
INSERT INTO r_pickup AS r (wf, sim_day, hour, cell, attempt_bucket, dials, connected)
SELECT wf, sim_day, hour, cell, attempt_bucket, count(*), count(*) FILTER (WHERE connected)
FROM calls WHERE cell IS NOT NULL
GROUP BY wf, sim_day, hour, cell, attempt_bucket
ORDER BY wf, sim_day, hour, cell, attempt_bucket
ON CONFLICT (wf, sim_day, hour, cell, attempt_bucket) DO UPDATE SET
    dials = r.dials + excluded.dials, connected = r.connected + excluded.connected;

-- name: r_did
INSERT INTO r_did AS r (wf, tick_no, hour, cohort_code, did, dials, connected)
SELECT wf, tick_no, hour, cohort_code, did, count(*), count(*) FILTER (WHERE connected)
FROM calls
GROUP BY wf, tick_no, hour, cohort_code, did
ORDER BY wf, tick_no, hour, cohort_code, did
ON CONFLICT (wf, tick_no, hour, cohort_code, did) DO UPDATE SET
    dials = r.dials + excluded.dials, connected = r.connected + excluded.connected;

-- name: r_flags
INSERT INTO r_flags AS r (wf, sim_hour, prompt, connected, flags, engaged)
SELECT wf, sim_hour, prompt, count(*), count(*) FILTER (WHERE flag),
       count(*) FILTER (WHERE engaged)
FROM calls WHERE connected
GROUP BY wf, sim_hour, prompt
ORDER BY wf, sim_hour, prompt
ON CONFLICT (wf, sim_hour, prompt) DO UPDATE SET
    connected = r.connected + excluded.connected, flags = r.flags + excluded.flags,
    engaged = r.engaged + excluded.engaged;

-- name: approval_events
INSERT INTO approval_event (event_id, approval_id, wf, status, reasons, minute)
SELECT e.event_id, e.approval_id, x.wf, coalesce(e.status, 'unknown'), e.reasons, e.minute
FROM ev e JOIN wfx x USING (workflow_id)
WHERE e.type = 'approval_decided'
ORDER BY e.event_id
ON CONFLICT (event_id) DO NOTHING;

-- name: other_events
INSERT INTO event_other (event_id, type, body)
SELECT event_id, coalesce(type, 'unknown'), body FROM ev
WHERE body IS NOT NULL
ORDER BY event_id
ON CONFLICT (event_id) DO NOTHING;

-- name: summary
SELECT (SELECT count(*) FROM calls),
       (SELECT count(*) FROM ev WHERE type = 'lead'),
       (SELECT count(*) FROM calls WHERE lead_sk IS NULL),
       (SELECT count(*) FROM calls WHERE w < 0 OR v < 0 OR did < 0 OR prompt < 0);

-- name: stage_delete
-- paths: sql
DELETE FROM stage_raw WHERE chunk_no = %(chunk_no)s;

-- name: chunk_done
INSERT INTO ingested_chunk (chunk_no, kind, events, first_event_id, last_event_id, until_minute,
                            bytes, txn_ms)
VALUES (%(chunk_no)s, %(kind)s, %(events)s, %(first_event_id)s, %(last_event_id)s,
        %(until_minute)s, %(bytes)s, %(txn_ms)s);
