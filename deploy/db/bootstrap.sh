#!/usr/bin/env bash
# Creates the aHoy database roles and databases (SPEC -> Data model -> Database roles).
# Run once by an admin (superuser or CREATEROLE + CREATEDB). Not run by Alembic.
#
# Locally: mounted into /docker-entrypoint-initdb.d/ by docker-compose.yml (runs on an empty
# data volume). CI and production: run it with psql connection settings in the standard libpq
# environment (PGHOST, PGPORT, PGUSER, PGPASSWORD).
#
# Needs AHOY_OWNER_PASSWORD and AHOY_APP_PASSWORD. Passwords are passed to psql as variables
# and quoted with :'var', never interpolated into SQL text. Safe to re-run: roles and databases
# are created only when missing; passwords and attributes are reset every run.
#
# Roles:
#   ahoy_owner  LOGIN CREATEDB  owns every table, runs Alembic (MIGRATION_DATABASE_URL).
#                               CREATEDB lets the test suite drop and recreate ahoy_test.
#   ahoy_app    LOGIN           the backend (DATABASE_URL); rights come from the migrations.
#   ahoy_purge  NOLOGIN         owns purge_sessions(); granted to ahoy_owner so migrations
#                               can hand the function to it.
set -eu

: "${AHOY_OWNER_PASSWORD:?AHOY_OWNER_PASSWORD must be set}"
: "${AHOY_APP_PASSWORD:?AHOY_APP_PASSWORD must be set}"

# Inside the official postgres image's init phase POSTGRES_USER is the superuser.
admin_user="${PGUSER:-${POSTGRES_USER:-postgres}}"

run_psql() {
    psql -X -v ON_ERROR_STOP=1 --username "$admin_user" "$@"
}

run_psql --dbname postgres \
    -v owner_pw="$AHOY_OWNER_PASSWORD" \
    -v app_pw="$AHOY_APP_PASSWORD" <<'SQL'
SELECT 'CREATE ROLE ahoy_owner'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ahoy_owner') \gexec
SELECT 'CREATE ROLE ahoy_app'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ahoy_app') \gexec
SELECT 'CREATE ROLE ahoy_purge'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ahoy_purge') \gexec

ALTER ROLE ahoy_owner WITH LOGIN CREATEDB NOSUPERUSER NOCREATEROLE PASSWORD :'owner_pw';
ALTER ROLE ahoy_app WITH LOGIN NOCREATEDB NOSUPERUSER NOCREATEROLE PASSWORD :'app_pw';
ALTER ROLE ahoy_purge WITH NOLOGIN NOCREATEDB NOSUPERUSER NOCREATEROLE PASSWORD NULL;

GRANT ahoy_purge TO ahoy_owner;

SELECT 'CREATE DATABASE ahoy OWNER ahoy_owner'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'ahoy') \gexec
SELECT 'CREATE DATABASE ahoy_test OWNER ahoy_owner'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'ahoy_test') \gexec
ALTER DATABASE ahoy OWNER TO ahoy_owner;
ALTER DATABASE ahoy_test OWNER TO ahoy_owner;

-- Only the aHoy roles may connect (the owner can always connect; granted for clarity).
REVOKE CONNECT ON DATABASE ahoy, ahoy_test FROM PUBLIC;
GRANT CONNECT ON DATABASE ahoy, ahoy_test TO ahoy_owner, ahoy_app;
SQL

# Postgres 15+ already omits CREATE on public for PUBLIC; revoke anyway so older
# templates and managed providers end up the same. ahoy_test may be recreated by the
# test suite from template1, which on 15+ has the same default.
for db in ahoy ahoy_test; do
    run_psql --dbname "$db" <<'SQL'
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
done

echo "aHoy roles and databases are ready."
