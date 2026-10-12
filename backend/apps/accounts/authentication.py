from django.core.exceptions import ValidationError
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied

from apps.core.db import clear_db_context, set_db_context
from apps.tenants.models import Tenant

from .models import Role, User
from .tokens import TokenError, decode_token

FAILED = "Invalid or expired credentials."

# Paths still usable while locked out, so the app can show the right screen.
LOCKOUT_ALLOWED = {"/api/auth/me/", "/api/auth/logout/", "/api/subscription/", "/api/billing/status/", "/api/billing/checkout/"}
PASSWORD_CHANGE_ALLOWED = {"/api/auth/me/", "/api/auth/logout/", "/api/auth/change-password/"}


def enforce_access_gates(user, path):
    """Account-level lock-outs, applied to normal API calls (not to login/refresh).

    * suspended tenant: owner and manager are locked out; attendants keep recording
      (grace period, so records have no gaps). cancelled tenant: everyone is locked out.
    * temporary password: nothing works until the user chooses their own.
    """
    if user.tenant_id:
        status = user.tenant.status
        locked = status == Tenant.Status.CANCELLED or (status == Tenant.Status.SUSPENDED and user.role in (Role.OWNER, Role.MANAGER))
        if locked and path not in LOCKOUT_ALLOWED:
            raise PermissionDenied({"detail": "This account is suspended. Please pay to reactivate.", "code": "tenant_suspended"})
    if user.must_change_password and path not in PASSWORD_CHANGE_ALLOWED:
        raise PermissionDenied({"detail": "Please choose a new password first.", "code": "password_change_required"})


def set_context_for_user(user):
    if user.role == Role.PLATFORM_ADMIN:
        set_db_context(platform_admin=True)
    else:
        set_db_context(tenant_id=user.tenant_id)


def user_from_claims(claims):
    """Resolve the user behind a verified token AND set the tenant database context.

    Order matters: the RLS context is set from the signed claims FIRST, then the user is
    loaded through RLS. A token that claims tenant B for a user of tenant A therefore finds
    no user and is rejected. Role, tenant and token_version are all re-verified.
    """
    user_id, role, tenant_claim, version = (claims.get(k) for k in ("user_id", "role", "tenant_id", "tv"))
    if not user_id or role not in Role.values or version is None:
        raise AuthenticationFailed(FAILED)
    try:
        if role == Role.PLATFORM_ADMIN:
            if tenant_claim:
                raise AuthenticationFailed(FAILED)
            set_db_context(platform_admin=True)
        else:
            if not tenant_claim:
                raise AuthenticationFailed(FAILED)
            set_db_context(tenant_id=tenant_claim)
        user = User.objects.select_related("tenant").filter(pk=user_id).first()
    except (ValueError, ValidationError):
        clear_db_context()
        raise AuthenticationFailed(FAILED)

    if (
        user is None
        or not user.is_active
        or user.role != role
        or str(user.tenant_id or "") != str(tenant_claim or "")
        or user.token_version != version
    ):
        clear_db_context()
        raise AuthenticationFailed(FAILED)
    return user


class TenantJWTAuthentication(BaseAuthentication):
    """Tenant-aware auth.

    This (not a Django middleware) sets the context because ATOMIC_REQUESTS opens the
    transaction around the *view*, i.e. inside every middleware. SET LOCAL only works inside
    that transaction, and DRF authenticates inside the view.

    Hand-rolled with PyJWT so we do not need django.contrib.auth / contenttypes (and their tables).
    """

    keyword = b"bearer"

    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if not parts or parts[0].lower() != self.keyword:
            return None
        if len(parts) != 2:
            raise AuthenticationFailed(FAILED)
        try:
            claims = decode_token(parts[1].decode(), "access")  # signature, expiry, token type
        except (TokenError, UnicodeDecodeError):
            raise AuthenticationFailed(FAILED)
        user = user_from_claims(claims)
        enforce_access_gates(user, request.path)
        return user, claims

    def authenticate_header(self, request):
        return "Bearer"
