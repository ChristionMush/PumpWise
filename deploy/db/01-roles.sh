#!/bin/sh
# Runs once, the first time the database volume is created.
# Creates the limited role the app runs as. It cannot bypass Row-Level Security, and it cannot DELETE or TRUNCATE:
# business data is never hard-deleted (PLAN.md rule 5); demo wipes use the owner role.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE pumpwise_app LOGIN PASSWORD '${APP_DB_PASSWORD}' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO pumpwise_app;
GRANT USAGE ON SCHEMA public TO pumpwise_app;
ALTER DEFAULT PRIVILEGES FOR ROLE ${POSTGRES_USER} IN SCHEMA public GRANT SELECT, INSERT, UPDATE ON TABLES TO pumpwise_app;
ALTER DEFAULT PRIVILEGES FOR ROLE ${POSTGRES_USER} IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO pumpwise_app;
SQL
