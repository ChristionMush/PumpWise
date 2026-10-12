# PumpWise

Built by Tendeka & Elichi. `PLAN.md` is the product plan. **All phases (0-11) are implemented.**
Know your numbers on your phone, and stop losing fuel and cash.

## What is in here
| Phase | What | Where |
|---|---|---|
| 1 | Tenancy, auth, roles, Row-Level Security, isolation test suite, audit log | `backend/apps/{core,tenants,accounts,audit}` |
| 2 | Platform admin screen (create business + owner + stations, subscription, suspend, reset password) | `apps/tenants`, `frontend/src/pages/Platform*` |
| 3 | Setup (stations, tanks + calibration, pumps incl. brand/model/year/photos/data_source, nozzles, prices with history), shift start/end, **offline sync**, QR codes, `DataSourceAdapter` | `apps/stations`, `apps/operations`, `frontend/src/offline.ts` |
| 4 | Daily sales per day / pump / fuel / currency, averages per day and weekday | `apps/reports/sales.py` |
| 5 | Reconciliation: tank loss (dips + deliveries vs pump meters), cash per shift by payment type | `apps/reports/reconciliation.py` |
| 6 | Owner dashboard (one small cached call), one-tap price update | `apps/insights/dashboard.py` |
| 7 | Forecast (simple + weekday-weighted), alerts, daily summary via provider interface, tenant settings/branding | `apps/insights` |
| 8 | Billing: provider interface, signed idempotent webhook, reminders (day 1/3/7), auto-suspension (day 10), instant reactivation, pricing model, reseller share | `apps/billing` |
| 9 | Per-tenant "Export my data" (CSV + JSON, time-limited link), retention policy | `apps/exports`, `docs/retention.md` |
| 10 | AWS (Terraform), seed demo data, runbook, security checklist | `infra/terraform`, `docs/` |
| 11 | Onboarding checklist, attendant tutorial, demo tenant | `apps/onboarding`, `frontend/src/components2.tsx` |

## Run locally
```bash
cp .env.example .env            # fill in the passwords
docker compose up --build       # db, redis, API :8000, worker, scheduler, web app :5173
docker compose exec backend sh -c "PLATFORM_ADMIN_PASSWORD='...' python manage.py create_platform_admin --name 'Your Name' --email you@example.com"
docker compose exec backend python manage.py seed_demo      # a demo business with 30 days of sample data (dev password: Demo-pass-2026)
```
Open http://localhost:5173. Demo logins are printed by `seed_demo`.

## Tests
```bash
cd backend && pip install -r requirements-dev.txt && DB_HOST=localhost DB_ADMIN_PASSWORD=postgres pytest     # ~210 tests, needs Postgres 16
cd frontend && npm ci && npm run typecheck && npm test && npm run build
```
The backend suite builds a fresh database with the same two-role setup as production and runs as the restricted `app_user`.
`tests/test_isolation_everything.py` seeds two full businesses and checks every tenant table for cross-tenant reads, writes and deletes.

## Key rules (see `PLAN.md` section 3)
Row-Level Security in the database (`docs/rls.md`) · prices keep `effective_from/to` and sales use the price in force · nothing is hard-deleted (the app DB role has no DELETE) and every change is audited · secrets only in environment/Secrets Manager.

## Docs
`docs/rls.md` isolation design · `docs/offline-sync.md` conflict rules · `docs/billing-providers.md` adding the payment company · `docs/device-ingestion.md` Tier 2 design note · `docs/runbook.md` operations · `docs/retention.md` · `docs/security-checklist.md`.

## Open decisions (yours)
1. **Payment provider**: not chosen. The pipeline is built behind `BillingProvider`; see `docs/billing-providers.md`.
2. **SMS / WhatsApp vendor**: not chosen. `MessageProvider` ships with a console provider and a generic HTTP-gateway provider.
3. **Shona and Ndebele**: `frontend/src/i18n.ts` has the structure; the files are empty until a native speaker translates.
4. **Tier 2 hardware**: nothing built or claimed (see `docs/device-ingestion.md`).
