# Tenant isolation design (Row-Level Security)

Isolation is enforced **in PostgreSQL**, not only in Django code. A bug in a view cannot leak
another tenant's data because the database refuses to return or change those rows.

## Two database roles, one database

| Role        | Used by                              | Can                                                    |
|-------------|--------------------------------------|--------------------------------------------------------|
| `app_user`  | the running app (`default` alias)    | SELECT / INSERT / UPDATE. **Never DELETE or TRUNCATE.** No superuser, no `BYPASSRLS`, owns nothing. |
| `app_owner` | migrations only (`owner` alias)      | Owns tables and changes schema. Also bound by RLS (`FORCE`). |
| superuser   | `manage.py bootstrap_db`, once       | Creates the two roles. The app never uses it.          |

Migrations: `python manage.py migrate --database=owner`. A router stops the runtime role from
migrating. Because `app_user` has no DELETE grant, "never hard-delete business data" is enforced
by the database too (soft delete = UPDATE of `deleted_at`).

## How the tenant is known to the database

Three transaction-local settings, set with `set_config(..., true)` (= `SET LOCAL`):

* `app.tenant_id` - the tenant this request acts for
* `app.platform_admin` - `on` for Tendeka & Elichi staff (control-plane tables only)
* `app.login_identifier` - only during login, so one account can be found before the tenant is known

They vanish when the transaction ends, so they cannot leak between requests on pooled connections.
`ATOMIC_REQUESTS=True` is therefore mandatory, and setting context outside a transaction raises.

**Request flow:** `TenantJWTAuthentication` verifies the signed token, sets the context from its
claims, then loads the user *through RLS* and re-checks role, tenant and `token_version`. A token
claiming tenant B for a tenant-A user finds no row and is rejected. (This lives in the DRF
authenticator rather than a Django middleware because the request transaction opens inside every
middleware.)

**Background jobs / commands / tests:** `with db_context(tenant_id=...):`

## Policies

Helper functions `app_tenant_id()`, `app_platform_admin()`, `app_login_identifier()` use `NULLIF`
because Postgres leaves `''` (not NULL) in a setting after a `SET LOCAL` ends. No context means
no rows, never an error and never all rows.

| Table            | Read                                                | Write                                  |
|------------------|-----------------------------------------------------|----------------------------------------|
| `tenants_tenant` | own row, or platform                                | platform only                          |
| `accounts_user`  | own tenant's users; platform staff rows in platform context; exactly one account during login | own tenant; platform staff in platform context |
| `tenants_subscription` | own row, or platform                          | platform only (a tenant can read, never write, its own subscription) |
| `stations_station` | own tenant only (standard `tenant_isolation`)     | own tenant only                        |
| `audit_auditlog` | own tenant (including what platform staff did inside it); platform sees platform-level rows plus rows flagged `via_platform` | append only (no UPDATE grant, no DELETE); only the platform may write a `via_platform` row |

A CHECK constraint makes `role = platform_admin` true **iff** `tenant_id IS NULL`, so a tenant user
cannot be created or promoted into a platform admin even via raw SQL.

Platform admins do **not** get a blanket bypass: their context exposes `tenants`, `subscriptions`
and platform staff rows only, never tenant users or business data (stations and everything after).

To create an owner login or a station for tenant X, a platform endpoint narrows the database context
to exactly that one tenant with `tenant_scope(X)` (platform flag stays on, `app.tenant_id = X`), does
the work, and restores the context. Each such action writes an audit row flagged `via_platform`, which
the tenant's owner sees as "Tendeka & Elichi" (never an individual staff name). Consequence: the
platform tenant list loads each row's owner and station count inside that row's own scope, so list
pages are kept small (25 by default, 50 max).

## Account lock-outs (`apps/accounts/authentication.py`)

Applied to normal API calls after the user is authenticated (not to login or refresh):

| Situation | Effect |
|---|---|
| Tenant `suspended` | Owner and manager get `403 {"code": "tenant_suspended"}`; attendants keep working (grace period so records have no gaps) |
| Tenant `cancelled` | Everyone in the tenant gets the same 403 |
| `must_change_password` | `403 {"code": "password_change_required"}` until the user changes it |

`/api/auth/me/`, `/api/auth/logout/` and `/api/subscription/` stay reachable while locked out, so the app
can show the "pay to reactivate" screen. Reactivating takes effect on the next request. Nothing is deleted.

## Adding a tenant-owned table (Phase 3+)

```python
from apps.core.models import TenantOwnedModel
from apps.core.rls import enable_rls, tenant_isolation

class Station(TenantOwnedModel): ...

# in the migration, after CreateModel (and dependencies = [("core", "0001_initial"), ...]):
enable_rls("stations_station", tenant_isolation())
```

`tests/test_rls_guard.py` fails CI if any table lacks ENABLE+FORCE RLS or a policy, if a
`tenant_id` table's policy does not use `app_tenant_id()`, or if `app_user` gains DELETE/TRUNCATE.

## Known limits (by design)

* RLS defends against application bugs. Arbitrary SQL injection could also call `set_config`; the
  mitigation for that is the ORM (parameterised queries), code review and least-privilege roles.
* Email and phone are unique across all tenants (login happens before the tenant is known), so a
  duplicate error reveals only that the value is taken, never whose it is.

## Tables added in Phases 3-11 (all strict `tenant_isolation`, no platform access)
stations: `stations_station`, `stationassignment`, `fueltype`, `tank`, `calibrationpoint`, `pump`, `nozzle`, `fuelprice`, `exchangerate`;
operations: `shift`, `pumpmeterreading`, `tankreading`, `delivery`, `cashsubmission`, `transaction`, `syncoperation`;
`tenants_tenantsettings`, `insights_alert`, `insights_outboundmessage`, `exports_tenantexport`.
Tenants read but cannot write: `tenants_subscription`, `billing_payment`, `billing_billingreminder`. Platform-only: `billing_reseller`, `billing_paymentevent`. The price list `billing_pricingplan` is readable by everyone signed in, writable by the platform.
`tests/test_isolation_everything.py` seeds two full demo businesses and checks EVERY table that has a `tenant_id`.

## Added with the current PLAN.md (rules 8-11, several attendants, Phase 12)
* `operations_shiftattendant`, `operations_pumpassignment` (people on a shift, pump holders) and the rollups
  `operations_dailysalesrollup`, `operations_tankstatus`, `operations_shiftsummary`: strict `tenant_isolation`.
* Paid feature grants live on `tenants_tenant` (`multi_attendant_enabled`, `pump_assignment_enabled`), which tenants can read
  but only the platform can update. The station policy columns (`attendants_mode`, `assignment_mode`) are guarded by the
  `station_policy_guard` trigger: outside the platform context they cannot change, and nobody can pick a mode the tenant has
  not been granted (tests/test_feature_grants.py tries it with raw SQL).
* Demo wipes (`apps/onboarding/demo.py: wipe_demo_data`) are the one place rows are deleted. They run on the owner
  connection, check `is_demo` in the database first, and set that tenant's context so FORCE RLS confines the delete to it.
  On the single-server kit the owner role is the container's superuser, which bypasses RLS; there the `is_demo` check and the
  `WHERE tenant_id = ...` are what protect other businesses (tests/test_demo_and_trials.py).
* Role rules (who may see dips, deliveries, tank levels, reconciliation) are not RLS: they live in one module,
  `apps/accounts/permissions.py`, and tests/test_rule11_tank_side.py checks every endpoint.
