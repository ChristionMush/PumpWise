#!/usr/bin/env bash
# Creates .env from .env.example with random secrets. Safe to run once.
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then echo ".env already exists. Delete it first if you really want a new one."; exit 1; fi
read -rp "Domain for this server (for example demo.yourdomain.com): " DOMAIN
case "$DOMAIN" in *[!a-zA-Z0-9.-]*|"") echo "Domain has invalid characters"; exit 1;; esac
PG=$(openssl rand -hex 20); APP=$(openssl rand -hex 20); SECRET=$(openssl rand -hex 32)
sed -e "s/__DOMAIN__/$DOMAIN/g" -e "s/__PG__/$PG/g" -e "s/__APP__/$APP/g" -e "s/__SECRET__/$SECRET/g" .env.example > .env
chmod 600 .env
echo ".env created. Now edit AWS_REGION and BACKUP_BUCKET (nano .env). Never commit .env to Git."
