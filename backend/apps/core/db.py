"""
Per-transaction database context for Row-Level Security.

The RLS policies read three transaction-local settings:

* ``app.tenant_id``        the tenant the current request/job acts for
* ``app.platform_admin``   'on' for Tendeka & Elichi staff (control-plane tables only)
* ``app.login_identifier`` only set during login, so ONE user row can be found before
                           the tenant is known

``set_config(..., true)`` is the function form of ``SET LOCAL``: values vanish when the
transaction ends, so they can never leak to another request on a pooled connection.
"""
import uuid
from contextlib import contextmanager

from django.db import connection, transaction


def _clean_tenant(tenant_id):
    if tenant_id in (None, ""):
        return ""
    return str(uuid.UUID(str(tenant_id)))  # raises ValueError on garbage


def set_db_context(*, tenant_id=None, platform_admin=False, login_identifier=None):
    if not connection.in_atomic_block:
        raise RuntimeError(
            "Database context must be set inside a transaction (SET LOCAL semantics). "
            "Use `with db_context(...):` or run inside an ATOMIC_REQUESTS request."
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.tenant_id', %s, true),"
            " set_config('app.platform_admin', %s, true),"
            " set_config('app.login_identifier', %s, true)",
            [_clean_tenant(tenant_id), "on" if platform_admin else "off", login_identifier or ""],
        )


def clear_db_context():
    set_db_context()


def get_db_context():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('app.tenant_id', true),"
            " current_setting('app.platform_admin', true),"
            " current_setting('app.login_identifier', true)"
        )
        tenant, platform, login = cursor.fetchone()
    return {
        "tenant_id": tenant or None,
        "platform_admin": platform == "on",
        "login_identifier": login or None,
    }


@contextmanager
def db_context(*, tenant_id=None, platform_admin=False, login_identifier=None):
    """Run a block (job, command, test) as a given tenant / platform context."""
    with transaction.atomic():
        previous = get_db_context()
        set_db_context(tenant_id=tenant_id, platform_admin=platform_admin, login_identifier=login_identifier)
        yield
        # Only reached on success; on error the atomic block rolls back and the GUC with it.
        set_db_context(**previous)


@contextmanager
def tenant_scope(tenant_id, *, platform_admin=True):
    """Temporarily act inside one tenant, inside the CURRENT transaction (no savepoint).

    Used by platform staff endpoints: the platform flag stays on, and the tenant context is
    narrowed to exactly one tenant. The previous context is restored on success; on error the
    surrounding transaction/savepoint rolls back, which reverts the setting with it.
    """
    if not connection.in_atomic_block:
        raise RuntimeError("tenant_scope must run inside a transaction")
    previous = get_db_context()
    set_db_context(tenant_id=tenant_id, platform_admin=platform_admin)
    yield
    set_db_context(**previous)
