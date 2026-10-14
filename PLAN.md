# PLAN.md: Fuel Station Management SaaS

Built by Tendeka & Elichi. Read this file fully before doing anything. Plan first, then code. Wait for my approval after each phase plan.

## 1. Product

A multi-tenant SaaS that lets fuel station owners and managers run their stations from a phone: pumps, tank levels, daily sales with changing prices, average litres used, next tank refill forecast, loss detection, and shift/cash accountability.

We sell visibility and control: "Know your numbers on your phone and stop losing fuel and cash."

**Primary market: Zimbabwe.** People there will not use anything tedious. Every screen must be fast, work offline, and need as few taps as possible. If a feature adds work for attendants, simplify it or automate it.

**Users**
- **Platform admin** (Tendeka & Elichi only): creates and suspends tenants, sees billing status.
- **Owner:** all stations in their tenant, phone-first dashboard, reports, user management.
- **Manager:** one or more stations. Records **tank dips and fuel deliveries**, assigns pumps, closes shifts, reviews reconciliation.
- **Attendant:** minimal-effort entry on a phone or tablet: pump meter readings and their own cash only. Must work offline. **No access to dips, deliveries, tank levels or reconciliation** (not even to view them).

## 2. Stack (do not change without asking)

- Backend: Django + Django REST Framework
- Database: PostgreSQL with Row-Level Security. If volume grows, use partitioned tables (pg_partman). TimescaleDB was not found in AWS's RDS extension lists, so only consider Timescale Cloud or a self-managed instance if automatic device data arrives (see HOSTING_STRATEGY.md section 4)
- Jobs: Celery + Redis (rollups, forecasts, alerts, billing reminders)
- Frontend: mobile-first PWA (installable, offline-capable with a sync queue)
- Local dev: Docker Compose
- Production: AWS (see section 8)
- Payments: abstract behind a `BillingProvider` interface (Stripe, Paystack, Flutterwave, or a local Zimbabwe provider). Confirm the provider with me before Phase 8.
- Branding: login screen, header, and report footers show "Built by Tendeka & Elichi"; each tenant can set its own logo and station names

## 3. Non-negotiable rules

1. Every tenant-owned table has `tenant_id`. Row-Level Security is enforced in the database, not only in code.
2. The app connects to Postgres as a non-superuser role that RLS applies to. Set the tenant context per request (`SET LOCAL app.tenant_id`).
3. Write automated tests proving tenant A can never read, update, or delete tenant B's data, through the API, the ORM, and raw SQL. These run in CI and must always pass.
4. Prices are stored with `effective_from` and `effective_to`, per station and fuel type. Sales always use the price active at the time of the transaction.
5. Never hard-delete business data. Use soft delete or status fields. Keep an audit log of who changed what.
6. Secrets only in environment variables or AWS Secrets Manager. Never commit secrets.
7. Small commits, tests with every feature, plan before code.
8. Every write from the PWA carries a client-generated UUID and is idempotent. Sync is one batch call per shift with a per-record result.
9. Dashboards read rollup tables written when a shift closes, never raw readings. Cache each tenant's dashboard for 30 to 60 seconds and show a "last updated" time.
10. Optional features (SMS, WhatsApp, photos, reports) must fail soft: the shift still saves.
11. Roles are enforced on the server, in one place (`app/fuel/permissions.py`), never only by hiding buttons. Dips (`TankDip`) and deliveries (`Delivery`) can be created and read only by managers (for their station) and owners. Attendants get a refusal on every dip, delivery, tank-level and reconciliation endpoint, and the API never includes those fields in an attendant's responses. Add a test for each endpoint. Dips and deliveries carry a client UUID (rule 8) and `recorded_by`.

## 4. Core data model

Tenant -> Station -> Tank -> Pump -> Nozzle -> Transaction

Plus: User (with role and tenant), FuelType, FuelPrice, TankReading (dip), PumpMeterReading, Delivery (fuel received), Shift, ShiftAttendant, CashSubmission, Alert, AuditLog, Subscription (status, plan, paid_through), TenantExport.

**Multiple attendants per shift (required from day one):**
- A `Shift` (station, date, type, status) has many attendants through `ShiftAttendant` (tenant_id, shift, user, `is_lead`, `joined_at`, `left_at`, optional pump assignment). Never store a single attendant on `Shift`.
- **Paid feature grants (platform owner only):** each tenant has two switches, `multi_attendant_enabled` and `pump_assignment_enabled` (assignment requires multiple attendants). The platform owner sets each station's `attendants_mode` (`single` or `multiple`) and `assignment_mode` at setup, and can only choose what the company has been granted. If a grant is removed (for example unpaid), stations fall back to one attendant and shared pumps; no data is deleted. Policy cannot change while a shift is open. Implemented in `app/fuel/services.py` (`set_tenant_features`, `set_station_policy`).
- Pump assignment is a **per-station setting** (`assignment_mode`: `shared` or `assigned`, default `shared`). `shared`: all attendants on a shift share all pumps and cash is reconciled at shift level. `assigned`: the manager (or lead) assigns pumps to attendants at shift start, so cash is reconciled per attendant. Pre-fill from the previous shift's assignment so starting a shift takes one tap.
- In `assigned` mode: a pump belongs to exactly one attendant at a time. Reassigning mid-shift (swap, break) is one tap and splits the pump's litres at a meter snapshot, so each person is charged only for their own period. Every change is audit-logged. Unassigned pumps are flagged before the shift can start.
- The owner dashboard and shift reports show shortages per attendant (with the pump and period) when assignment is on, and per shift when it is off. Reports must use neutral wording such as "cash difference", not accusations.
- Meter readings stay **per nozzle per shift**, entered once by anyone on the shift (store `recorded_by`). Do not make each attendant re-enter them. If a second person enters a different value for the same nozzle and reading, keep both and raise a "readings differ" flag for the lead or manager. Never silently overwrite.
- Cash: each attendant submits their own `CashSubmission` (by payment type). With pump assignment, expected cash is per attendant (litres from their pumps x active price). Without it, reconcile the sum of all submissions against the shift total and show each person's submission beside it.
- Closing a shift is done by the lead or a manager, and is allowed once every attendant on it has submitted or been marked as left.
- Mid-shift handover: an attendant leaving sets `left_at`. A new attendant joins with `joined_at`. Optional handover meter snapshot (later).
- Offline: two phones on one shift each queue writes with client UUIDs (rule 8). Sync merges them. Shift totals are computed from the merged data, never from one phone.
- Migration: move any existing single attendant on `Shift` into `ShiftAttendant` with a data migration, reversible.

**Pump fields (required from day one):**
- `brand` (text, optional at first)
- `model` (text, optional at first)
- `year` (integer, optional)
- `nameplate_photo` and `display_photo` (optional image uploads, for later hardware research)
- `data_source` (enum: `manual`, `pulse_reader`, `controller`; default `manual`)

**Tank fields:** capacity, reserve level, fuel type, calibration chart (dip height to litres), optional `data_source` (`manual` or `atg`).

**Payment types for cash reconciliation:** cash (per currency), mobile money (e.g. EcoCash), card/swipe, credit/coupon. Store the currency on every sale and price.

## 5. Two data tiers (design for both, build Tier 1 first)

**Tier 1: Manual, lightweight (MVP, works at any station).**
**Attendant** effort must be about 2 minutes per shift (meters and cash only):
- Shift start: tap the pump, enter or photograph the meter number for each nozzle.
- Shift end: same again, plus enter the amounts handed in by payment type.
- The app calculates litres x the price active at the time and instantly shows expected vs. handed in.
- Prefill everything possible. Big buttons, numeric keypad, minimal typing, local-language labels.
- Optional photo of the meter as evidence. Meter-photo number recognition can be added later.

**Manager** effort (about 3 minutes per dip round, plus a delivery when fuel arrives):
- Dips: pick the tank, type the dipstick reading (or the dip height, converted with the tank's calibration chart), save. Taken at each shift change by default, or once a day if the station chooses. Optional photo of the dipstick.
- Deliveries: pick the tank, enter litres, supplier and delivery note number. Optional photo of the delivery note. A delivery is recorded by the manager (or owner) who received it, never by an attendant.
- If the manager is away, the owner can record dips and deliveries. There is no attendant fallback by design: the person who sells fuel does not also measure what is left in the tank.

**Tier 2: Automatic (later premium upgrade, per-station hardware).**
Pump and tank data arrives with no attendant input (pulse readers, forecourt controllers, automatic tank gauges). Do NOT build this now. Instead:
- Build a `DataSourceAdapter` interface that converts any device feed into the same internal `PumpMeterReading` / `TankReading` / `Transaction` records. The Tier 1 manual flow is the first implementation of this interface.
- Dispatch by each pump's and tank's `data_source` field, so adding an automatic adapter later needs no change to the core model, reconciliation, or dashboard.
- Add an ingestion endpoint design note (authenticated device tokens per station, idempotent writes) in the docs, but do not implement it yet.
- Do not promise or claim any specific pump brand support. Pump models will be researched after the system is running.

## 6. Phases

**Phase 0: Review my prototype.** Read the existing prototype in this repo. List what to keep, what to rewrite, and why. Propose the Phase 1 plan. Stop and wait for approval.

**Phase 1: Tenancy and auth.** Tenant model, user model, roles (owner/manager/attendant/platform admin), login, tenant-aware middleware, RLS policies, and the isolation test suite.

**Phase 2: Platform admin screen.** Only Tendeka & Elichi can access it. Create a tenant, its owner login, and its stations. Show each tenant's subscription status.

**Phase 3: Station setup and manual entry.** Stations, tanks, pumps (with the pump fields in section 4), nozzles, fuel prices with history, and the lightweight attendant shift start/end flow from section 5 (meters and cash only). Manager screens for tank dips and deliveries (rule 11), with tests that attendants are refused. Offline entry with sync. The `DataSourceAdapter` interface with the manual implementation. Shifts support several attendants at once (see section 4), including joining and leaving mid-shift, with tests for two phones syncing the same shift offline.

**Phase 4: Daily sales.** Sales per day, per pump, per fuel type, per currency, using the price active at the time. Average litres used per day and per weekday.

**Phase 5: Reconciliation.** Compare litres dispensed (pump meters, from attendants) against tank level drops plus deliveries (dips and deliveries, from managers). Because they come from different people, a mismatch is a stronger signal. Flag unexplained loss above a configurable threshold. Cash check: expected vs. submitted per shift, broken down by payment type. With several attendants, also show submitted per attendant, and per-attendant expected cash when pumps are assigned.

**Phase 6: Owner dashboard (phone-first).** Today's sales, tank levels, next refill date, unexplained loss, shift status, across all the owner's stations. Fast on slow connections. One-tap price update (new price applies from a chosen time).

**Phase 7: Forecast, alerts, and daily summary.** Days to empty = (current level - reserve) / average daily usage. Start simple, then add day-of-week weighting. Alerts: low stock, loss anomalies, missed readings. Send a daily summary to the owner by WhatsApp or SMS (behind a provider interface) so they only open the app when something is wrong. Tenant branding settings.

**Phase 8: Billing and auto-suspension.**
- Subscription statuses: `active`, `past_due`, `suspended`, `cancelled`.
- Payment webhook (verify signatures, idempotent) updates status. A successful payment for a new subscription auto-creates the tenant, owner account, and welcome invite.
- Reminders at day 1, 3, and 7 past due (SMS/WhatsApp/email plus in-app banner), via Celery.
- At about day 10: suspend. Owner and manager dashboards lock behind a "pay to reactivate" screen. Attendants can still record sales for a grace period so records have no gaps.
- Paying reactivates instantly. Never delete data on suspension; retain at least 90 days.
- Pricing model supports: one-time setup fee per station, monthly fee per station, multi-station discount, optional add-ons, and a configurable revenue share for a reseller partner.

**Phase 9: Data safety and export.** Per-tenant "Export my data" button (CSV and JSON written to S3, delivered by a time-limited link). Retention and deletion policy documented.

**Phase 10: AWS deployment.** Follow HOSTING_STRATEGY.md (stages, load tests, alarms, backup drills, go-live checklist) and section 8. Staging first, then production. Seed demo data. Security review checklist before real tenants.

**Phase 11: Pilot readiness.** Onboarding checklist screen for the setup technician, a short in-app tutorial for attendants, and a demo tenant with sample data for sales presentations.

**Phase 12: Demo environment and trial tenants.**
- Add `is_demo` to Tenant. Demo tenants never send real SMS, WhatsApp, or email, and use the billing provider's sandbox.
- Write a `seed_demo` management command that loads `mock_data/seed_demo.json` (made by `generate_mock_data.py`) through the ORM so RLS applies. It must be idempotent and only ever wipe and reload tenants where `is_demo = true`.
- Nightly Celery job: regenerate the mock data with `--end` set to yesterday and reload the demo tenants, so the demo always looks live. Add a test proving this job can never touch a non-demo tenant.
- Platform admin action "Create trial tenant": takes a prospect name, creates a demo tenant from the template with an owner login (random password) and a 14-day expiry. Expired trials are suspended, and deleted after 30 days if not converted.
- Show a visible "Demo data" banner in the UI on demo tenants.
- Add a health check endpoint, a Docker Compose file for a single-server demo, and an AWS budget alarm.
- See DEMO_HOSTING_PLAN.md for the hosting design.

## 7. Zimbabwe and usability requirements

- Offline-first for attendants: queue entries locally, sync when connectivity returns, show clear sync status, resolve conflicts safely. Power cuts and weak signal are normal.
- Lightweight: runs well on cheap Android phones and small data bundles. Keep page weight low.
- Multi-currency (e.g. USD and ZiG): price and record sales per currency, with an owner-set exchange rate where needed for totals.
- Mobile money (e.g. EcoCash) as a first-class payment type.
- Large buttons, minimal typing, plain language, local-language label support (English first, structure for Shona and Ndebele).
- Optional photo upload of a meter (attendant) or a dipstick reading and delivery note (manager) as evidence.
- QR codes on pumps and tanks so attendants open the right entry screen in one scan.

## 8. AWS deployment (Phase 10)

- Database: Amazon RDS for PostgreSQL, automated backups (14 to 35 days), point-in-time restore, Multi-AZ once there are paying tenants, encrypted at rest.
- Cross-region copy of snapshots. S3 bucket with versioning for monthly exports.
- App: ECS Fargate (or Elastic Beanstalk if simpler). Redis via ElastiCache. Secrets in AWS Secrets Manager.
- TLS everywhere. Private subnets for the database.
- CloudWatch alarms: downtime, failed backup, high CPU, low disk, error spikes.
- Infrastructure as code (Terraform). Separate staging and production.
- Provide a runbook: how to restore a backup, how to export one tenant's data, how to suspend and reactivate a tenant manually.

## 9. Definition of done for each phase

- All tests pass, including the tenant isolation suite.
- Migrations included and reversible.
- Short README update.
- A summary of what changed and anything I should review by hand.

## 10. Start

Do Phase 0 now. Then propose a detailed plan for Phase 1 and wait for my approval.
