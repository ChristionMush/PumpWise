#!/usr/bin/env bash
# Safe deploy: backup, pull, build, migrate, restart, smoke test.
# Do not run during shift changes (05:30-06:30, 13:30-14:30, 21:30-22:30 Zimbabwe time).
set -euo pipefail
cd "$(dirname "$0")"
APP_DIR=$(grep -E '^APP_SRC=' .env 2>/dev/null | cut -d= -f2- || true)
APP_DIR="${APP_DIR:-../backend}"

if ! git -C "$APP_DIR" rev-parse --git-dir > /dev/null 2>&1; then
  echo "$APP_DIR is not a Git checkout, so there is nothing to pull. Deploy from your PC with deploy/push-to-aws.ps1 instead."
  exit 1
fi

echo "1/5 Backup"; ./backup.sh
echo "2/5 Pull latest code"; git -C "$APP_DIR" pull --ff-only
echo "3/5 Build"; docker compose build
echo "4/5 Migrate and restart"; docker compose run --rm migrate; docker compose up -d
echo "5/5 Smoke test"; sleep 10; ./smoke.sh
docker image prune -f > /dev/null
echo "Deployed. To roll back: git -C $APP_DIR checkout <previous-commit>, then run this script again."
echo "If a migration went wrong, restore the backup taken in step 1 (see README)."
