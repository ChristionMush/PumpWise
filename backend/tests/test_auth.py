import pytest
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from apps.accounts.models import Role, User
from apps.audit.models import AuditLog
from apps.core.db import db_context

from .helpers import PASSWORD, client_for, make_tenant, make_user, two_tenants


def login(identifier, password=PASSWORD):
    return APIClient().post("/api/auth/login/", {"identifier": identifier, "password": password}, format="json")


def test_login_by_email_is_case_insensitive_and_returns_tokens_and_branding():
    t = make_tenant()
    make_user(t, Role.OWNER, email="Boss@Example.com")
    res = login("  boss@example.COM ")
    assert res.status_code == 200
    body = res.json()
    assert body["access"] and body["refresh"] and body["user"]["role"] == "owner"
    assert body["branding"]["built_by"] == "Tendeka & Elichi"
    assert "password" not in body["user"]


@pytest.mark.parametrize("typed", ["0771234567", "+263771234567", "263 77 123 4567", "077-123-4567"])
def test_login_by_zimbabwe_phone_in_any_common_format(typed):
    t = make_tenant()
    make_user(t, Role.ATTENDANT, phone="+263771234567")
    assert login(typed).status_code == 200


def test_wrong_password_unknown_user_inactive_and_deleted_all_look_identical():
    t = make_tenant()
    make_user(t, Role.OWNER, email="ok@example.com")
    inactive = make_user(t, Role.MANAGER, email="inactive@example.com")
    gone = make_user(t, Role.MANAGER, email="gone@example.com")
    with db_context(tenant_id=t.id):
        User.objects.filter(pk=inactive.pk).update(is_active=False)
        User.objects.get(pk=gone.pk).deactivate()
    responses = [login("ok@example.com", "wrong"), login("nobody@example.com"), login("inactive@example.com"), login("gone@example.com"), login("not an identifier!")]
    assert {r.status_code for r in responses} == {401}
    assert len({r.content for r in responses}) == 1


def test_login_does_not_expose_other_accounts():
    """The login-lookup RLS exception must reveal nothing beyond the one matching account."""
    a, b = two_tenants()
    res = login(a[Role.OWNER].email)
    assert res.status_code == 200 and res.json()["user"]["tenant_id"] == str(a["tenant"].id)


def test_me_and_tenant_info():
    t = make_tenant("Harare Fuel")
    owner = make_user(t, Role.OWNER)
    body = client_for(owner).get("/api/auth/me/").json()
    assert body["user"]["id"] == str(owner.id) and body["tenant"]["name"] == "Harare Fuel"


def test_refresh_flow_and_token_type_separation():
    t = make_tenant()
    make_user(t, Role.OWNER, email="r@example.com")
    tokens = login("r@example.com").json()
    c = APIClient()
    assert c.post("/api/auth/refresh/", {"refresh": tokens["refresh"]}, format="json").status_code == 200
    # an access token is not a refresh token, and vice versa
    assert c.post("/api/auth/refresh/", {"refresh": tokens["access"]}, format="json").status_code == 401
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['refresh']}")
    assert c.get("/api/auth/me/").status_code == 401
    assert APIClient().post("/api/auth/refresh/", {"refresh": "garbage"}, format="json").status_code == 401


def test_logout_revokes_all_existing_tokens():
    t = make_tenant()
    make_user(t, Role.OWNER, email="l@example.com")
    tokens = login("l@example.com").json()
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
    assert c.post("/api/auth/logout/").status_code == 204
    assert c.get("/api/auth/me/").status_code == 401
    assert APIClient().post("/api/auth/refresh/", {"refresh": tokens["refresh"]}, format="json").status_code == 401


def test_change_password_signs_out_other_sessions_but_keeps_this_one():
    t = make_tenant()
    make_user(t, Role.OWNER, email="p@example.com")
    old = login("p@example.com").json()
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {old['access']}")
    bad = c.post("/api/auth/change-password/", {"current_password": "nope", "new_password": "An0ther-good-pw"}, format="json")
    assert bad.status_code == 400
    res = c.post("/api/auth/change-password/", {"current_password": PASSWORD, "new_password": "An0ther-good-pw"}, format="json")
    assert res.status_code == 200
    assert c.get("/api/auth/me/").status_code == 401  # old access token is dead
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {res.json()['access']}")
    assert c.get("/api/auth/me/").status_code == 200  # fresh token works
    assert login("p@example.com", PASSWORD).status_code == 401
    assert login("p@example.com", "An0ther-good-pw").status_code == 200


def test_deactivated_user_loses_access_immediately():
    t = make_tenant()
    owner = make_user(t, Role.OWNER)
    attendant = make_user(t, Role.ATTENDANT)
    att_client = client_for(attendant)
    assert att_client.get("/api/auth/me/").status_code == 200
    assert client_for(owner).delete(f"/api/users/{attendant.id}/").status_code == 204
    assert att_client.get("/api/auth/me/").status_code == 401


def test_login_is_rate_limited(monkeypatch):
    monkeypatch.setitem(ScopedRateThrottle.THROTTLE_RATES, "login", "3/min")
    codes = [login("nobody@example.com").status_code for _ in range(5)]
    assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]


def test_weak_passwords_are_refused():
    t = make_tenant()
    owner = make_user(t, Role.OWNER)
    res = client_for(owner).post("/api/users/", {"full_name": "X", "email": "x@example.com", "role": "attendant", "password": "12345"}, format="json")
    assert res.status_code == 400


def test_failed_login_for_known_user_is_audited_and_unknown_is_not():
    t = make_tenant()
    make_user(t, Role.OWNER, email="aud@example.com")
    login("aud@example.com", "wrong")
    login("ghost@example.com", "wrong")
    with db_context(tenant_id=t.id):
        assert list(AuditLog.objects.values_list("action", flat=True)) == ["login_failed"]


def test_health_endpoint():
    assert APIClient().get("/health/").json() == {"status": "ok", "version": "dev"}
