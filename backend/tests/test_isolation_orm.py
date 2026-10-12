"""Tenant A can never read, update or delete tenant B's data - through the ORM."""
import pytest
from django.db import DatabaseError, transaction

from apps.accounts.models import Role, User
from apps.audit import services as audit
from apps.audit.models import AuditLog
from apps.core.db import db_context
from apps.tenants.models import Tenant

from .helpers import make_platform_admin, two_tenants


def test_read_only_own_users():
    a, b = two_tenants()
    with db_context(tenant_id=a["tenant"].id):
        ids = set(User.all_objects.values_list("id", flat=True))
    assert ids == {a[Role.OWNER].id, a[Role.MANAGER].id, a[Role.ATTENDANT].id}


def test_cannot_get_other_tenants_user_by_pk():
    a, b = two_tenants()
    with db_context(tenant_id=a["tenant"].id):
        with pytest.raises(User.DoesNotExist):
            User.objects.get(pk=b[Role.OWNER].pk)
        assert not User.all_objects.filter(pk=b[Role.OWNER].pk).exists()


def test_cannot_update_other_tenants_user():
    a, b = two_tenants()
    with db_context(tenant_id=a["tenant"].id):
        assert User.objects.filter(pk=b[Role.MANAGER].pk).update(full_name="HACKED") == 0
        assert User.all_objects.update(full_name="HACKED") == 3  # only A's three users
    with db_context(tenant_id=b["tenant"].id):
        assert not User.objects.filter(full_name="HACKED").exists()


def test_cannot_delete_other_tenants_user():
    a, b = two_tenants()
    with db_context(tenant_id=a["tenant"].id):
        assert User.objects.filter(pk=b[Role.MANAGER].pk).delete() == 0  # soft delete: 0 rows touched
    with db_context(tenant_id=b["tenant"].id):
        assert User.objects.filter(pk=b[Role.MANAGER].pk).exists()


def test_cannot_insert_row_for_other_tenant():
    a, b = two_tenants()
    with db_context(tenant_id=a["tenant"].id):
        with pytest.raises(DatabaseError):
            with transaction.atomic():
                User.objects.create_user(tenant_id=b["tenant"].id, role=Role.OWNER, full_name="Evil", email="evil@example.com", password="x" * 12)


def test_cannot_move_own_user_into_other_tenant():
    a, b = two_tenants()
    with db_context(tenant_id=a["tenant"].id), pytest.raises(DatabaseError):
        with transaction.atomic():
            User.objects.filter(pk=a[Role.MANAGER].pk).update(tenant_id=b["tenant"].id)


def test_no_context_means_no_rows():
    two_tenants()
    with transaction.atomic():
        assert User.all_objects.count() == 0
        assert Tenant.all_objects.count() == 0
        assert AuditLog.objects.count() == 0


def test_tenant_sees_only_its_own_tenant_row_and_cannot_write_tenants():
    a, b = two_tenants()
    with db_context(tenant_id=a["tenant"].id):
        assert list(Tenant.all_objects.values_list("id", flat=True)) == [a["tenant"].id]
        assert Tenant.objects.filter(pk=b["tenant"].pk).update(name="HACKED") == 0
        assert Tenant.objects.filter(pk=a["tenant"].pk).update(name="HACKED") == 0  # tenants cannot rename themselves
        with pytest.raises(DatabaseError), transaction.atomic():
            Tenant.objects.create(name="Rogue", slug="rogue")


def test_platform_admin_sees_all_tenants_but_not_tenant_users():
    a, b = two_tenants()
    admin = make_platform_admin()
    with db_context(platform_admin=True):
        assert Tenant.all_objects.count() == 2
        users = list(User.all_objects.values_list("id", flat=True))
    assert users == [admin.id]  # platform context never exposes tenant staff


def test_audit_log_is_tenant_scoped_and_append_only():
    a, b = two_tenants()
    with db_context(tenant_id=a["tenant"].id):
        audit.log("test_a", actor=a[Role.OWNER])
    with db_context(tenant_id=b["tenant"].id):
        audit.log("test_b", actor=b[Role.OWNER])
    with db_context(tenant_id=a["tenant"].id):
        assert list(AuditLog.objects.values_list("action", flat=True)) == ["test_a"]
        with pytest.raises(DatabaseError):  # cannot write an audit row for tenant B
            with transaction.atomic():
                audit.log("forged", actor=a[Role.OWNER], tenant_id=b["tenant"].id)
        # DB refuses UPDATE (no grant) ...
        with pytest.raises(DatabaseError), transaction.atomic():
            AuditLog.objects.update(action="tampered")
        # ... and the model refuses delete/update too
        entry = AuditLog.objects.get()
        with pytest.raises(RuntimeError):
            entry.delete()
        entry.action = "x"
        with pytest.raises(RuntimeError):
            entry.save()
