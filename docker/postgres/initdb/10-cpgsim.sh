#!/usr/bin/env bash
# First start of an empty data directory only (docker-entrypoint-initdb.d):
#   1. a self-signed server certificate and `ssl = on`;
#   2. pg_hba: trust on the container's own Unix socket, scram-sha-256 over SSL
#      for every TCP connection, plain (non-SSL) TCP rejected;
#   3. the roles sim_writer and bi_reader with their passwords (sql/roles.sql).
# The generator then connects as sim_writer and `sim init` creates the schemas.
# Changing passwords later: ALTER ROLE ... PASSWORD (this script never runs again).
set -euo pipefail

: "${CPGSIM_WRITER_PASSWORD:?CPGSIM_WRITER_PASSWORD must be set}"
: "${CPGSIM_READER_PASSWORD:?CPGSIM_READER_PASSWORD must be set}"

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

cat > "$PGDATA/pg_hba.conf" <<'HBA'
# cpgsim: written at first start by docker/postgres/initdb/10-cpgsim.sh
# TYPE     DATABASE  USER  ADDRESS    METHOD
local      all       all              trust
hostssl    all       all   0.0.0.0/0  scram-sha-256
hostssl    all       all   ::/0       scram-sha-256
hostnossl  all       all   0.0.0.0/0  reject
hostnossl  all       all   ::/0       reject
HBA

echo "cpgsim: creating roles"
psql -v ON_ERROR_STOP=1 --no-psqlrc --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v writer_password="$CPGSIM_WRITER_PASSWORD" \
    -v reader_password="$CPGSIM_READER_PASSWORD" \
    -f /cpgsim/roles.sql
