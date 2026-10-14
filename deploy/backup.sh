#!/usr/bin/env bash
# Nightly database backup to S3. Checks the dump is readable before uploading.
set -euo pipefail
cd "$(dirname "$0")"
envval() { grep -E "^$1=" .env | head -1 | cut -d= -f2-; }
BUCKET=$(envval BACKUP_BUCKET); REGION=$(envval AWS_REGION)
PGUSER=$(envval POSTGRES_USER); PGDB=$(envval POSTGRES_DB); PING=$(envval HEALTHCHECK_URL)
[ -n "$BUCKET" ] || { echo "BACKUP_BUCKET is empty in .env"; exit 1; }

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
FILE=$(mktemp /tmp/pumpwise-XXXXXX.dump)
trap 'rm -f "$FILE"' EXIT

docker compose exec -T db pg_dump -U "$PGUSER" -d "$PGDB" -Fc > "$FILE"
[ -s "$FILE" ] || { echo "Dump is empty"; exit 1; }
docker compose exec -T db pg_restore --list < "$FILE" > /dev/null

AWS_DEFAULT_REGION="$REGION" aws s3 cp "$FILE" "s3://$BUCKET/db/$(date -u +%Y/%m)/pumpwise-$STAMP.dump" --only-show-errors
echo "backup ok $STAMP ($(du -h "$FILE" | cut -f1))"
if [ -n "$PING" ]; then curl -fsS -m 10 --retry 3 "$PING" > /dev/null || true; fi
