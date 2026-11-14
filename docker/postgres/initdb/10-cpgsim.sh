#!/usr/bin/env bash
# First start of an empty data directory only (docker-entrypoint-initdb.d):
#   1. a self-signed server certificate and `ssl = on`;
#   2. pg_hba scoped by role: trust on the container's own Unix socket only
#      (postgres superuser: docker exec, no TCP rule); bi_reader over SSL from
#      anywhere; sim_writer over SSL from the Compose network only (samenet);
#      everything else, and any non-SSL TCP, rejected;
#   3. the roles sim_writer and bi_reader with their passwords (sql/roles.sql).
# The generator then connects as sim_writer and `sim init` creates the schemas.
# Changing passwords later: ALTER ROLE ... PASSWORD (this script never runs again).
set -euo pipefail

: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}"
: "${CPGSIM_WRITER_PASSWORD:?CPGSIM_WRITER_PASSWORD must be set}"
: "${CPGSIM_READER_PASSWORD:?CPGSIM_READER_PASSWORD must be set}"

# Refuse weak or placeholder passwords. NOTE: the official entrypoint does not
# remove a half-initialised data directory when an init script fails, so after
# fixing .env you must recreate the volume: docker compose down -v
check_password() {
    local name="$1" value="$2"
    case "${value,,}" in
        change-me|changeme|password|secret|postgres|"") weak=1 ;;
        *) weak=0 ;;
    esac
    if [ "$weak" = 1 ] || [ "${#value}" -lt 16 ]; then
        echo "cpgsim: $name is a placeholder or shorter than 16 characters." >&2
        echo "cpgsim: set strong values in .env (openssl rand -base64 24), then run" >&2
        echo "cpgsim: 'docker compose down -v' to discard this half-initialised volume." >&2
        exit 1
    fi
}
check_password POSTGRES_PASSWORD "$POSTGRES_PASSWORD"
check_password CPGSIM_WRITER_PASSWORD "$CPGSIM_WRITER_PASSWORD"
check_password CPGSIM_READER_PASSWORD "$CPGSIM_READER_PASSWORD"
if [ "$POSTGRES_PASSWORD" = "$CPGSIM_WRITER_PASSWORD" ] || [ "$POSTGRES_PASSWORD" = "$CPGSIM_READER_PASSWORD" ] \
    || [ "$CPGSIM_WRITER_PASSWORD" = "$CPGSIM_READER_PASSWORD" ]; then
    echo "cpgsim: the three passwords must differ (then 'docker compose down -v')." >&2
    exit 1
fi

echo "cpgsim: generating a self-signed TLS certificate"
openssl req -new -x509 -days 3650 -nodes -newkey rsa:2048 \
    -subj "/CN=${CPGSIM_TLS_CN:-cpgsim-postgres}" \
    -keyout "$PGDATA/server.key" -out "$PGDATA/server.crt" 2>/dev/null
chmod 600 "$PGDATA/server.key"

cat >> "$PGDATA/postgresql.conf" <<'CONF'

# -- cpgsim (docker/postgres/initdb/10-cpgsim.sh) --
ssl = on
ssl_cert_file = 'server.crt'
ssl_key_file = 'server.key'
ssl_min_protocol_version = 'TLSv1.2'
password_encryption = 'scram-sha-256'
CONF

# Unquoted heredoc on purpose: $POSTGRES_DB must expand.
# samenet is not airtight: connections relayed by Docker's userland proxy
# (Docker Desktop) arrive from the bridge gateway, which is in samenet. Bind
# the published port to a trusted interface (CPGSIM_PG_BIND) or firewall it.
cat > "$PGDATA/pg_hba.conf" <<HBA
# cpgsim: written at first start by docker/postgres/initdb/10-cpgsim.sh
# TYPE     DATABASE        USER        ADDRESS    METHOD
local      all             all                    trust
hostssl    $POSTGRES_DB    sim_writer  samenet    scram-sha-256
hostssl    $POSTGRES_DB    bi_reader   0.0.0.0/0  scram-sha-256
hostssl    $POSTGRES_DB    bi_reader   ::/0       scram-sha-256
host       all             all         0.0.0.0/0  reject
host       all             all         ::/0       reject
HBA

echo "cpgsim: creating roles"
psql -v ON_ERROR_STOP=1 --no-psqlrc --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v writer_password="$CPGSIM_WRITER_PASSWORD" \
    -v reader_password="$CPGSIM_READER_PASSWORD" \
    -f /cpgsim/roles.sql
