#!/usr/bin/env bash
# Restore drill: loads a backup into a scratch database (restore_test) and prints row counts.
# It never touches the live database. Usage: ./restore.sh [latest | s3-key]
set -euo pipefail
cd "$(dirname "$0")"
envval() { grep -E "^$1=" .env | head -1 | cut -d= -f2-; }
BUCKET=$(envval BACKUP_BUCKET); REGION=$(envval AWS_REGION); PGUSER=$(envval POSTGRES_USER)
export AWS_DEFAULT_REGION="$REGION"
KEY="${1:-latest}"
if [ "$KEY" = "latest" ]; then
  KEY=$(aws s3 ls "s3://$BUCKET/db/" --recursive | sort | tail -1 | awk '{print $4}')
fi
[ -n "$KEY" ] || { echo "No backups found in s3://$BUCKET/db/"; exit 1; }
echo "Restoring $KEY"
FILE=$(mktemp /tmp/pumpwise-restore-XXXXXX.dump)
trap 'rm -f "$FILE"' EXIT
aws s3 cp "s3://$BUCKET/$KEY" "$FILE" --only-show-errors

docker compose exec -T db psql -U "$PGUSER" -d postgres -c "DROP DATABASE IF EXISTS restore_test"
docker compose exec -T db psql -U "$PGUSER" -d postgres -c "CREATE DATABASE restore_test"
docker compose exec -T db pg_restore -U "$PGUSER" -d restore_test --no-owner < "$FILE"
docker compose exec -T db psql -U "$PGUSER" -d restore_test -c "ANALYZE"
docker compose exec -T db psql -U "$PGUSER" -d restore_test -c "SELECT relname AS table, n_live_tup AS rows FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 15"
echo "Restore drill finished. Compare the counts with the live system, then drop the scratch copy:"
echo "docker compose exec -T db psql -U $PGUSER -d postgres -c 'DROP DATABASE restore_test'"
