from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsOwnerOrManager, IsPlatformAdmin
from apps.audit import services as audit
from apps.core.db import tenant_scope
from apps.tenants.services import get_settings

from .checklist import MANUAL_FLAGS, build_checklist


class ChecklistView(APIView):
    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        return Response(build_checklist(request.user.tenant_id))


class FlagView(APIView):
    """Tick off a manual step (e.g. 'QR codes printed')."""

    permission_classes = [IsOwnerOrManager]

    def post(self, request):
        key, value = request.data.get("key"), bool(request.data.get("value", True))
        if key not in MANUAL_FLAGS:
            raise ValidationError({"key": "Unknown step."})
        cfg = get_settings(request.user.tenant_id)
        cfg.onboarding_flags = {**cfg.onboarding_flags, key: value}
        cfg.save(update_fields=["onboarding_flags", "updated_at"])
        audit.log("onboarding_flag", actor=request.user, obj=cfg, changes={key: value}, request=request)
        return Response(build_checklist(request.user.tenant_id))


class PlatformChecklistView(APIView):
    """The setup technician (platform staff) checks any tenant's progress. Narrowed to that one tenant."""

    permission_classes = [IsPlatformAdmin]

    def get(self, request, pk):
        from apps.tenants.views import _get_tenant

        tenant = _get_tenant(pk)
        if tenant is None:
            raise NotFound()
        with tenant_scope(tenant.id):
            return Response(build_checklist(tenant.id))
