#!/usr/bin/env bash
# Quick check that the public site answers, and that every container is up.
set -euo pipefail
cd "$(dirname "$0")"
DOMAIN=$(grep -E '^DOMAIN=' .env | cut -d= -f2-)
docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Health}}'
code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "https://$DOMAIN/health" || true)
if [ "$code" = "200" ]; then echo "OK: https://$DOMAIN/health returned 200"; else echo "FAIL: got HTTP $code"; exit 1; fi
