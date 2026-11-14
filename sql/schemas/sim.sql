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

-- Live switches from `sim scenario enable|disable`.
CREATE TABLE IF NOT EXISTS sim.scenario_overrides (
    id           serial      PRIMARY KEY,
    scenario_id  text        NOT NULL,
    action       text        NOT NULL CHECK (action IN ('enable', 'disable')),
    at           timestamptz NOT NULL,             -- simulation time the switch takes effect
    params       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_wall timestamptz NOT NULL DEFAULT now()
);
