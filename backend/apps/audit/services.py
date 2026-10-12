from django.conf import settings

from .models import AuditLog

# Never written to the audit log, even as a before/after diff.
SENSITIVE_FIELDS = {"password", "token_version"}


def client_ip(request):
    if request is None:
        return None
    if settings.TRUST_X_FORWARDED_FOR:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


def snapshot(obj, fields):
    return {f: _plain(getattr(obj, f)) for f in fields if f not in SENSITIVE_FIELDS}


def diff(before, after):
    """{field: [old, new]} for fields that changed."""
    return {k: [before.get(k), after.get(k)] for k in after if before.get(k) != after.get(k)}


def _plain(value):
    return value if value is None or isinstance(value, (bool, int, float, str)) else str(value)


def log(action, *, actor=None, tenant_id=None, obj=None, changes=None, request=None, via_platform=None):
    """Write one audit row inside the current transaction (so a rolled-back request leaves
    no phantom audit entry, and the tenant RLS context applies)."""
    if tenant_id is None and actor is not None:
        tenant_id = actor.tenant_id
    if via_platform is None:
        via_platform = actor is not None and actor.role == "platform_admin" and tenant_id is not None
    return AuditLog.objects.create(
        tenant_id=tenant_id,
        via_platform=via_platform,
        actor=actor,
        action=action,
        object_type=obj.__class__.__name__ if obj is not None else "",
        object_id=str(obj.pk) if obj is not None else "",
        changes=changes or {},
        ip_address=client_ip(request),
    )
