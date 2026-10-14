"""Permanently delete one business and everything in it: the manual, audited deletion in docs/retention.md.

Meant for test and demo businesses. A real business is refused unless the caller passes allow_real=True,
and even then only after its owner has been offered a final export. The app never calls this; only the
`purge_tenant` management command does.

Runs on the owner connection in one transaction. Some tables have no DELETE policy at all (the business row,
its users, subscription and audit log are append-only for the app), so Row-Level Security is lifted for this
transaction only; every statement is still limited by `WHERE tenant_id = <this business>`, and before commit
every table is re-counted: anything left over rolls the whole purge back.
"""
import json
import uuid

from django.db import connections, transaction


class NotDemoTenant(Exception):
    """Raised before anything is deleted when asked to purge a real business without allow_real."""


def _tenant_tables(cur):
    cur.execute("SELECT DISTINCT table_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND column_name = 'tenant_id' ORDER BY 1")
    return [t for (t,) in cur.fetchall()]


def _count(cur, table, column, tid):
    cur.execute(f'SELECT count(*) FROM "{table}" WHERE "{column}" = %s', [tid])
    return cur.fetchone()[0]


def list_tenants():
    with transaction.atomic(using="owner"), connections["owner"].cursor() as cur:
        cur.execute("SELECT set_config('app.platform_admin', 'on', true)")
        cur.execute("SELECT id, name, slug, is_demo, trial_expires_at, status, created_at FROM tenants_tenant ORDER BY created_at")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def find_tenant(slug_or_id):
    for t in list_tenants():
        if slug_or_id in (t["slug"], str(t["id"])):
            return t
    return None


def purge_tenant(tenant_id, *, allow_real=False, dry_run=True):
    """Returns {"name", "slug", "is_demo", "rows": {table: count}}. With dry_run nothing changes."""
    tid = str(tenant_id)
    with transaction.atomic(using="owner"), connections["owner"].cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, true), set_config('app.platform_admin', 'on', true)", [tid])
        cur.execute("SELECT name, slug, is_demo FROM tenants_tenant WHERE id = %s", [tid])
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No business with id {tid}")
        name, slug, is_demo = row
        if not is_demo and not allow_real:
            raise NotDemoTenant(f"{name} ({slug}) is a real business, not a demo")

        tables = _tenant_tables(cur)
        # Lift FORCE ROW LEVEL SECURITY for this transaction only (ALTER TABLE is transactional), so the owner can
        # delete from append-only tables; put it back exactly as it was before commit.
        cur.execute("SELECT relname FROM pg_class WHERE relname = ANY(%s) AND relforcerowsecurity",
                    [tables + ["tenants_tenant"]])
        forced = [t for (t,) in cur.fetchall()]
        for t in forced:
            cur.execute(f'ALTER TABLE "{t}" NO FORCE ROW LEVEL SECURITY')

        rows = {}
        for t in tables:
            n = _count(cur, t, "tenant_id", tid)
            if n:
                rows[t] = n
        rows["tenants_tenant"] = 1

        if not dry_run:
            for t in tables:  # foreign keys are checked at commit, so the order does not matter
                cur.execute(f'DELETE FROM "{t}" WHERE tenant_id = %s', [tid])
            cur.execute("DELETE FROM tenants_tenant WHERE id = %s", [tid])
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")  # a missed reference fails here, and nothing is deleted
            left = {t: n for t in tables if (n := _count(cur, t, "tenant_id", tid))}
            if left or _count(cur, "tenants_tenant", "id", tid):
                raise RuntimeError(f"Purge incomplete, rolled back: {left}")
            cur.execute(
                "INSERT INTO audit_auditlog (id, tenant_id, actor_id, action, object_type, object_id, changes, via_platform, created_at) "
                "VALUES (%s, NULL, NULL, 'tenant_purged', 'tenant', %s, %s, true, now())",
                [str(uuid.uuid4()), tid, json.dumps({"name": name, "slug": slug, "is_demo": is_demo, "rows": rows})],
            )

        for t in forced:
            cur.execute(f'ALTER TABLE "{t}" FORCE ROW LEVEL SECURITY')
        if dry_run:
            transaction.set_rollback(True, using="owner")
    return {"name": name, "slug": slug, "is_demo": is_demo, "rows": rows}
