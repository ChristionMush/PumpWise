# PumpWise

Built by Tendeka & Elichi. `PLAN.md` is the product plan. **All phases (0-12) are implemented against the current PLAN.md** (rules 1-11, several attendants per shift, demo and trial businesses).
Know your numbers on your phone, and stop losing fuel and cash.

## What is in here
| Phase | What | Where |
|---|---|---|
| 1 | Tenancy, auth, roles, Row-Level Security, isolation test suite, audit log | `backend/apps/{core,tenants,accounts,audit}` |
| 2 | Platform admin screen (create business + owner + stations, subscription, suspend, reset password) | `apps/tenants`, `frontend/src/pages/Platform*` |
| 3 | Setup (stations, tanks + calibration, pumps incl. brand/model/year/photos/data_source, nozzles, prices with history), shift start/end, **offline sync** (one batch per shift), QR codes, `DataSourceAdapter`. **Several attendants per shift** (join/leave, per-person cash, close when everyone is done, readings that differ are kept and flagged, assigned pumps with one-tap swap). Dips and deliveries for managers/owners only | `apps/stations`, `apps/operations`, `frontend/src/offline.ts`, `frontend/src/pages/Shift.tsx` |
| 4 | Daily sales per day / pump / fuel / currency, averages per day and weekday | `apps/reports/sales.py` |
| 5 | Reconciliation: tank loss (dips + deliveries vs pump meters), cash per shift by payment type, per attendant (expected per person on assigned-pump stations), neutral "cash difference" wording | `apps/reports/reconciliation.py` |
| 6 | Owner dashboard read from rollups written when a shift closes (rule 9), cached 45 s per tenant with "updated" time, one-tap price update | `apps/insights/dashboard.py`, `apps/operations/rollups.py` |
| 7 | Forecast (simple + weekday-weighted), alerts, daily summary via provider interface, tenant settings/branding | `apps/insights` |
| 8 | Billing: provider interface, signed idempotent webhook, reminders (day 1/3/7), auto-suspension (day 10), instant reactivation, pricing model, reseller share | `apps/billing` |
| 9 | Per-tenant "Export my data" (CSV + JSON, time-limited link), retention policy | `apps/exports`, `docs/retention.md` |
| 10 | AWS (Terraform), seed demo data, runbook, security checklist | `infra/terraform`, `docs/` |
| 11 | Onboarding checklist, attendant tutorial, demo tenant | `apps/onboarding`, `frontend/src/components2.tsx` |
| 12 | Demo businesses (`is_demo`: no real messages, billing sandbox, banner), `mock_data/generate_mock_data.py` + `seed_demo`, nightly reload, "Create trial" with 14-day expiry, health check, demo compose file, AWS budget alarm | `apps/onboarding/demo.py`, `backend/mock_data/`, `deploy/docker-compose.demo.yml`, `infra/terraform/monitoring.tf` |

### Where the plan's names live
| PLAN.md says | In this repo |
|---|---|
| `app/fuel/permissions.py` (rule 11: every role rule in one place) | `backend/apps/accounts/permissions.py` |
| `app/fuel/services.py` (`set_tenant_features`, `set_station_policy`) | `backend/apps/tenants/features.py` |
| `TankDip` | `TankReading` (`backend/apps/operations/models.py`) |
| `mock_data/seed_demo.json`, `generate_mock_data.py` | `backend/mock_data/` (inside the image, so the nightly job can regenerate it) |

## Run locally
```bash
cp .env.example .env            # fill in the passwords
docker compose up --build       # db, redis, API :8000, worker, scheduler, web app :5173
docker compose exec backend sh -c "PLATFORM_ADMIN_PASSWORD='...' python manage.py create_platform_admin --name 'Your Name' --email you@example.com"
docker compose exec backend python manage.py seed_demo      # the demo business from mock_data/seed_demo.json (dev password: Demo-pass-2026); --generate refreshes the data up to yesterday
```
Open http://localhost:5173. Demo logins are printed by `seed_demo`.

## Tests
```bash
cd backend && pip install -r requirements-dev.txt && DB_HOST=localhost DB_ADMIN_PASSWORD=postgres pytest     # ~310 tests, needs Postgres 16
cd frontend && npm ci && npm run typecheck && npm test && npm run build
```
The backend suite builds a fresh database with the same two-role setup as production and runs as the restricted `app_user`.
`tests/test_isolation_everything.py` seeds two full businesses and checks every tenant table for cross-tenant reads, writes and deletes.

## Key rules (see `PLAN.md` section 3)
Row-Level Security in the database (`docs/rls.md`) · prices keep `effective_from/to` and sales use the price in force · nothing is hard-deleted (the app DB role has no DELETE) and every change is audited · secrets only in environment/Secrets Manager · every phone write carries a client UUID · dashboards read rollups · optional features fail soft · roles enforced on the server in one module.

## Docs
`docs/rls.md` isolation design · `docs/offline-sync.md` conflict rules · `docs/billing-providers.md` adding the payment company · `docs/device-ingestion.md` Tier 2 design note · `docs/runbook.md` operations · `docs/retention.md` · `docs/security-checklist.md`.

## Open decisions (yours)
1. **Payment provider**: not chosen. The pipeline is built behind `BillingProvider`; see `docs/billing-providers.md`.
2. **SMS / WhatsApp vendor**: not chosen. `MessageProvider` ships with a console provider and a generic HTTP-gateway provider.
3. **Shona and Ndebele**: `frontend/src/i18n.ts` has the structure; the files are empty until a native speaker translates.
4. **Tier 2 hardware**: nothing built or claimed (see `docs/device-ingestion.md`).
