"""Tenant A can never read, update or delete tenant B's data - through the HTTP API.

Note the users endpoint has NO tenant filter in its queryset; these pass because of RLS.
"""
import time

import jwt
from django.conf import settings
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.accounts.tokens import issue_tokens
from apps.audit import services as audit
from apps.core.db import db_context

from .helpers import PASSWORD, client_for, make_platform_admin, two_tenants


def test_list_returns_only_own_tenant():
    a, b = two_tenants()
    res = client_for(a[Role.OWNER]).get("/api/users/")
    assert res.status_code == 200
    assert {u["id"] for u in res.json()["results"]} == {str(a[r].id) for r in (Role.OWNER, Role.MANAGER, Role.ATTENDANT)}


def test_other_tenants_user_is_404_for_read_update_delete():
    a, b = two_tenants()
    client, victim = client_for(a[Role.OWNER]), b[Role.MANAGER]
    url = f"/api/users/{victim.id}/"
    assert client.get(url).status_code == 404
    assert client.patch(url, {"full_name": "HACKED"}, format="json").status_code == 404
    assert client.delete(url).status_code == 404
    with db_context(tenant_id=b["tenant"].id):
        fresh = User.objects.get(pk=victim.pk)
    assert fresh.full_name != "HACKED" and fresh.deleted_at is None and fresh.is_active


def test_created_user_always_lands_in_callers_tenant_even_if_body_says_otherwise():
    a, b = two_tenants()
    res = client_for(a[Role.OWNER]).post(
        "/api/users/",
        {"full_name": "New Hand", "email": "new@example.com", "role": "attendant", "password": PASSWORD, "tenant": str(b["tenant"].id), "tenant_id": str(b["tenant"].id)},
        format="json",
    )
    assert res.status_code == 201
    assert res.json()["tenant_id"] == str(a["tenant"].id)
    with db_context(tenant_id=b["tenant"].id):
        assert not User.objects.filter(email="new@example.com").exists()


def test_forged_tenant_claim_is_rejected():
    """Even a correctly signed token whose tenant claim does not match the user is useless."""
    a, b = two_tenants()
    claims = jwt.decode(issue_tokens(a[Role.OWNER])["access"], settings.JWT_SIGNING_KEY, algorithms=["HS256"])
    claims["tenant_id"] = str(b["tenant"].id)  # attacker (with the key) points the A owner at tenant B
    forged = jwt.encode(claims, settings.JWT_SIGNING_KEY, algorithm="HS256")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {forged}")
    assert client.get("/api/users/").status_code == 401


def test_token_signed_with_wrong_key_or_none_algorithm_is_rejected():
    a, _ = two_tenants()
    claims = jwt.decode(issue_tokens(a[Role.OWNER])["access"], settings.JWT_SIGNING_KEY, algorithms=["HS256"])
    for token in (
        jwt.encode(claims, "some-other-key-that-is-long-enough-0123456789", algorithm="HS256"),
        jwt.encode(claims, key=None, algorithm="none"),
    ):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        assert client.get("/api/users/").status_code == 401


def test_expired_token_is_rejected():
    a, _ = two_tenants()
    claims = jwt.decode(issue_tokens(a[Role.OWNER])["access"], settings.JWT_SIGNING_KEY, algorithms=["HS256"])
    claims["exp"] = int(time.time()) - 10
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION="Bearer " + jwt.encode(claims, settings.JWT_SIGNING_KEY, algorithm="HS256"))
    assert client.get("/api/users/").status_code == 401


def test_unauthenticated_requests_are_rejected():
    two_tenants()
    client = APIClient()
    for url in ("/api/users/", "/api/audit/", "/api/auth/me/", "/api/platform/tenants/"):
        assert client.get(url).status_code == 401, url


def test_audit_trail_is_tenant_scoped_via_api():
    a, b = two_tenants()
    with db_context(tenant_id=a["tenant"].id):
        audit.log("only_a", actor=a[Role.OWNER])
    with db_context(tenant_id=b["tenant"].id):
        audit.log("only_b", actor=b[Role.OWNER])
    actions = [e["action"] for e in client_for(a[Role.OWNER]).get("/api/audit/").json()["results"]]
    assert "only_a" in actions and "only_b" not in actions


def test_duplicate_email_across_tenants_is_rejected_without_leaking_whose_it_is():
    a, b = two_tenants()
    taken = b[Role.OWNER].email
    res = client_for(a[Role.OWNER]).post(
        "/api/users/", {"full_name": "Dup", "email": taken, "role": "manager", "password": PASSWORD}, format="json"
    )
    assert res.status_code == 400
    assert taken not in res.content.decode() and "tenant" not in res.content.decode().lower()


def test_platform_admin_lists_all_tenants_but_cannot_use_tenant_endpoints():
    a, b = two_tenants()
    client = client_for(make_platform_admin())
    res = client.get("/api/platform/tenants/")
    assert res.status_code == 200 and res.json()["count"] == 2
    assert client.get("/api/users/").status_code == 403
    assert client.get("/api/audit/").status_code == 403
