-- Ground truth: what the simulator planted. Never granted to analysts of the other schemas.
-- Append-mostly tables (no is_deleted); scenario_windows.end_at may be updated when a window closes.
CREATE SCHEMA IF NOT EXISTS truth;

CREATE TABLE IF NOT EXISTS truth.scenario_windows (
    window_id    text PRIMARY KEY,                -- e.g. carrier_decay:config:0
    scenario_id  text        NOT NULL,
    params       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    start_at     timestamptz NOT NULL,
    end_at       timestamptz,                     -- NULL while the window is open
    source       text        NOT NULL,            -- config | cli
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS scenario_windows_scenario_idx ON truth.scenario_windows (scenario_id);
CREATE INDEX IF NOT EXISTS scenario_windows_updated_at_idx ON truth.scenario_windows (updated_at);

CREATE TABLE IF NOT EXISTS truth.planted_effects (
    effect_id     text PRIMARY KEY,               -- deterministic unique key
    scenario_id   text        NOT NULL,           -- scenario id (or chaos defect id)
    window_id     text,
    entity_type   text        NOT NULL,           -- web_order | delivery | customer | sku | ...
    entity_id     text        NOT NULL,           -- neutral simulator id as text
    system_refs   jsonb       NOT NULL DEFAULT '{}'::jsonb,
    effect        text        NOT NULL,
    magnitude     double precision,
    detail        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    effective_at  timestamptz NOT NULL,
    created_at    timestamptz NOT NULL,
    updated_at    timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS planted_effects_scenario_entity_idx ON truth.planted_effects (scenario_id, entity_type);
CREATE INDEX IF NOT EXISTS planted_effects_updated_at_idx ON truth.planted_effects (updated_at);

COMMENT ON COLUMN truth.planted_effects.entity_id IS 'Neutral simulator id (not any system key); see system_refs for the per-system keys.';
COMMENT ON COLUMN truth.planted_effects.system_refs IS 'Keys of the affected entity in each source system (shop UUIDs, SAP numbers, WMS ids).';
COMMENT ON COLUMN truth.scenario_windows.end_at IS 'NULL while the scenario window is still open.';
COMMENT ON COLUMN truth.scenario_windows.source IS 'config | cli | chaos';
