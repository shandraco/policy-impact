#!/bin/sh
# Runs once, when the database volume is first created. The API connects as pi_app: not the table owner,
# no BYPASSRLS, so row-level security always applies to it.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v app_password="$PI_APP_PASSWORD" <<'SQL'
CREATE ROLE pi_app LOGIN PASSWORD :'app_password' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
SQL
