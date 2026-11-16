-- Roles for commerce-simulator (manual setup without Docker).
--
-- Run once as an admin (superuser or a role with CREATEROLE), connected to the
-- simulator's database. Idempotent: safe to run again.
--
--   psql "$ADMIN_DSN" -v writer_password='...' -v reader_password='...' -f sql/roles.sql
--
-- Both password variables are optional; without them the roles are created
-- without passwords (set them later with ALTER ROLE ... PASSWORD). Then, as
-- sim_writer:
--
--   sim --dsn "postgresql://sim_writer@host/dbname" init
--
-- `sim init` creates the schemas (owned by sim_writer), tables and indexes and
-- grants bi_reader USAGE + SELECT on the six business schemas (shop, wms, erp,
-- mkt, feeds, shelf), including tables created later. bi_reader gets nothing
-- on sim or truth.
--
-- The Docker setup does the same at the Postgres container's first start
-- (docker/postgres/initdb/10-cpgsim.sh).

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sim_writer') THEN
        CREATE ROLE sim_writer LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bi_reader') THEN
        CREATE ROLE bi_reader LOGIN;
    END IF;
END
$$;

-- The generator writes; it never needs to manage roles or databases.
ALTER ROLE sim_writer NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
-- The reader only reads.
ALTER ROLE bi_reader NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;

-- sim_writer creates its own schemas in this database.
DO $$
BEGIN
    -- Nothing by default (PUBLIC gets CONNECT and TEMP otherwise).
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
    -- TEMPORARY: the generator's sink and entity loaders use CREATE TEMP TABLE.
    EXECUTE format('GRANT CONNECT, CREATE, TEMPORARY ON DATABASE %I TO sim_writer', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO bi_reader', current_database());
END
$$;

-- Passwords (psql variables cannot be used inside DO blocks, hence \if).
\if :{?writer_password}
ALTER ROLE sim_writer PASSWORD :'writer_password';
\endif
\if :{?reader_password}
ALTER ROLE bi_reader PASSWORD :'reader_password';
\endif
