-- Simulator internals. Never granted to bi_reader.
CREATE SCHEMA IF NOT EXISTS sim;

CREATE TABLE IF NOT EXISTS sim.clock (
    id                  smallint PRIMARY KEY CHECK (id = 1),
    sim_time            timestamptz NOT NULL,      -- last committed simulation time
    history_start       timestamptz NOT NULL,
    seed                bigint      NOT NULL,
    config_fingerprint  text        NOT NULL,
    started_wall        timestamptz NOT NULL,
    updated_wall        timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS sim.event_queue (
    key       text        PRIMARY KEY,             -- deterministic, unique
    due       timestamptz NOT NULL,
    priority  smallint    NOT NULL,
    kind      text        NOT NULL,
    data      jsonb       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS event_queue_due_idx ON sim.event_queue (due, priority, key);

CREATE TABLE IF NOT EXISTS sim.entity_state (
    kind  text  NOT NULL,
    id    text  NOT NULL,
    data  jsonb NOT NULL,
    PRIMARY KEY (kind, id)
);

-- Both state tables are rewritten on every commit (upserts, deletes). Free
-- space on each page lets an upsert stay on its page (HOT update, no index
-- entry, pruned without VACUUM); autovacuum runs after 5 % instead of 20 %
-- dead rows. The engine also vacuums both during a backfill (runner.py,
-- VACUUM_EVERY_STATE_ROWS). Applied on every init, so existing tables get it too.
ALTER TABLE sim.entity_state SET (
    fillfactor = 70,
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.05
);
ALTER TABLE sim.event_queue SET (
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.05
);

CREATE TABLE IF NOT EXISTS sim.run_log (
    id           bigserial   PRIMARY KEY,
    wall_time    timestamptz NOT NULL,
    mode         text        NOT NULL,             -- init | backfill | live
    sim_from     timestamptz NOT NULL,
    sim_to       timestamptz NOT NULL,
    events       integer     NOT NULL,
    rows         jsonb       NOT NULL,             -- rows touched per schema
    queue_depth  integer     NOT NULL,
    duration_ms  integer     NOT NULL
);
CREATE INDEX IF NOT EXISTS run_log_wall_idx ON sim.run_log (wall_time);

-- Config changes on a running simulation (sim.clock keeps the fingerprint the
-- simulation started with; each change to a new fingerprint is recorded once).
CREATE TABLE IF NOT EXISTS sim.config_history (
    id               bigserial   PRIMARY KEY,
    wall_time        timestamptz NOT NULL DEFAULT now(),
    sim_time         timestamptz NOT NULL,         -- clock when the changed config was first used
    old_fingerprint  text        NOT NULL,
    new_fingerprint  text        NOT NULL
);

-- Live switches from `sim scenario enable|disable`.
CREATE TABLE IF NOT EXISTS sim.scenario_overrides (
    id           serial      PRIMARY KEY,
    scenario_id  text        NOT NULL,
    action       text        NOT NULL CHECK (action IN ('enable', 'disable')),
    at           timestamptz NOT NULL,             -- simulation time the switch takes effect
    params       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_wall timestamptz NOT NULL DEFAULT now()
);

-- Operator settings that survive `sim reset` (e.g. `seed`, set by `sim reset --seed N`
-- and applied by the CLI on top of the config).
CREATE TABLE IF NOT EXISTS sim.settings (
    key    text PRIMARY KEY,
    value  text NOT NULL
);
