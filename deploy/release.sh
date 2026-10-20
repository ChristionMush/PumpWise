#!/usr/bin/env bash
# Install one release on the server and restart Pumpwise. Normally started by push-to-aws.ps1 from your PC.
#
# A release is a copy of the code unpacked in /opt/pumpwise/releases/<commit>. This script runs from that copy:
#   1. refuses to run during shift changes (FORCE=1 overrides)
#   2. backs up the database (skipped only on a first install, when there is no database yet)
#   3. copies the release into /opt/pumpwise (deploy/.env, your settings, is never touched)
#   4. builds, migrates as the owner role, checks every table exists, restarts, smoke-tests
#
# Roll back to an earlier release (code only; a database change is undone by restoring the step-2 backup):
#   bash /opt/pumpwise/releases/<old-commit>/deploy/release.sh <old-commit>
set -euo pipefail

REL=$(cd "$(dirname "$0")/.." && pwd)
ROOT=${PUMPWISE_ROOT:-/opt/pumpwise}
SHA=${1:-$(basename "$REL")}
DEPLOY="$ROOT/deploy"
say() { printf '\n== %s\n' "$*"; }
envval() { grep -E "^$1=" "$DEPLOY/.env" | head -1 | cut -d= -f2-; }

# 1. Shift changes: 06:30-07:30 and 17:30-18:30 Zimbabwe time (day shift 07:00-18:00, night shift 18:00-07:00).
now=$(TZ=Africa/Harare date +%H%M)
for w in "0630 0730" "1730 1830"; do
  set -- $w
  if [ "$now" -ge "$1" ] && [ "$now" -le "$2" ]; then
    if [ "${FORCE:-0}" != "1" ]; then
      echo "It is $now in Zimbabwe, inside a shift change ($1-$2). Not deploying. Try later, or rerun with -Force."
      exit 1
    fi
    echo "Inside a shift change ($1-$2), deploying anyway because FORCE=1."
  fi
done

[ -f "$DEPLOY/.env" ] || {
  echo "No $DEPLOY/.env yet. First install: copy the deploy folder, run ./init-env.sh there, then deploy again."
  echo "(The release is unpacked at $REL; you can run:  mkdir -p $DEPLOY && cp -r $REL/deploy/. $DEPLOY/ && cd $DEPLOY && ./init-env.sh)"
  exit 1
}
command -v rsync > /dev/null || { echo "Installing rsync"; sudo apt-get install -y -qq rsync; }

# 2. Backup, while the old release is still running.
cd "$DEPLOY"
if [ -n "$(docker compose ps -q db 2> /dev/null)" ]; then
  if [ "${SKIP_BACKUP:-0}" = "1" ]; then
    say "2/6 Backup SKIPPED (SKIP_BACKUP=1)"
  else
    say "2/6 Backup"; bash ./backup.sh
  fi
else
  say "2/6 Backup: no database running yet (first install), nothing to back up"
fi

# 3. Install the code. backend/ and frontend/ are replaced exactly; everything else is updated in place,
#    so .env, logs and anything else you keep on the server stay.
if [ "$REL" != "$ROOT" ]; then
  say "3/6 Install release $SHA into $ROOT"
  # Keep a copy of the deploy folder as it was (hand edits on the server included), in case something there mattered.
  SAVED="$ROOT/releases/deploy-before-$SHA-$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$SAVED" && chmod 700 "$SAVED" && cp -a "$DEPLOY/." "$SAVED/"
  echo "Previous deploy folder saved in $SAVED"
  rsync -a --exclude '/releases/' --exclude '/deploy/.env' "$REL/" "$ROOT/"
  for d in backend frontend; do
    [ -d "$REL/$d" ] && rsync -a --delete "$REL/$d/" "$ROOT/$d/"
  done
  chmod +x "$DEPLOY"/*.sh "$DEPLOY"/db/*.sh
fi
# The old kit built the app from ../app; this repo keeps it in ../backend.
sed -i '/^APP_SRC=\.\.\/app$/d' .env
if grep -q '^APP_VERSION=' .env; then sed -i "s/^APP_VERSION=.*/APP_VERSION=$SHA/" .env; else echo "APP_VERSION=$SHA" >> .env; fi

# 4. Build and migrate.
say "4/6 Build"
docker compose up -d db redis
for _ in $(seq 1 30); do
  [ "$(docker compose ps db --format '{{.Health}}')" = "healthy" ] && break
  sleep 2
done
docker compose build

say "5/6 Migrate (owner role) and check the database"
docker compose run --rm -T migrate
# A migration can be recorded without being applied (the old kit's plain `migrate` did this). Check the tables really exist.
docker compose run --rm -T migrate python manage.py shell -c "
from django.apps import apps
from django.db import connections
have = set(connections['owner'].introspection.table_names())
miss = sorted(m._meta.db_table for m in apps.get_models() if m._meta.managed and not m._meta.proxy and m._meta.db_table not in have)
print('All tables present.' if not miss else 'MISSING TABLES: ' + ', '.join(miss))
raise SystemExit(1 if miss else 0)
"
# The app role never deletes business data (PLAN.md rule 5). New databases are created that way; this fixes older ones.
docker compose exec -T db psql -q -U "$(envval POSTGRES_USER)" -d "$(envval POSTGRES_DB)" \
  -c "REVOKE DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public FROM pumpwise_app;" \
  || echo "Warning: could not revoke DELETE from pumpwise_app (check the role exists)."

# 6. Restart and check it answers.
say "6/6 Restart and smoke test"
# No --remove-orphans: a container started by hand on the server must never be removed by a deploy.
docker compose up -d
ok=0
for _ in $(seq 1 12); do
  sleep 10
  if bash ./smoke.sh > /dev/null 2>&1; then ok=1; break; fi
done
bash ./smoke.sh || true
docker image prune -f > /dev/null

mkdir -p "$ROOT/releases"
if [ "$ok" = "1" ]; then
  echo "$(date -u +%FT%TZ) $SHA ok" >> "$ROOT/releases/history.log"
  echo "$SHA" > "$ROOT/releases/CURRENT"
  # Keep the five newest releases for rollback.
  ls -1dt "$ROOT"/releases/*/ 2> /dev/null | grep -v -e "/$SHA/\$" -e "/deploy-before-" | tail -n +5 | xargs -r rm -rf
  ls -1dt "$ROOT"/releases/deploy-before-*/ 2> /dev/null | tail -n +6 | xargs -r rm -rf
  say "Deployed $SHA. https://$(envval DOMAIN)/health now reports version $SHA."
else
  echo "$(date -u +%FT%TZ) $SHA FAILED" >> "$ROOT/releases/history.log"
  say "Deployed $SHA but the smoke test FAILED. See: docker compose logs --tail 100 web"
  prev=$(cat "$ROOT/releases/CURRENT" 2> /dev/null || true)
  [ -n "$prev" ] && echo "Roll back with: bash $ROOT/releases/$prev/deploy/release.sh $prev"
  exit 1
fi
