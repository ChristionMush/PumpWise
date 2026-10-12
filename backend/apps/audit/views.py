from rest_framework.generics import ListAPIView

from apps.accounts.permissions import IsOwner, IsPlatformAdmin

from .models import AuditLog
from .serializers import AuditLogSerializer, PlatformAuditSerializer


class AuditLogListView(ListAPIView):
    """An owner reads their own tenant's audit trail. Scoping is done by RLS."""

    permission_classes = [IsOwner]
    serializer_class = AuditLogSerializer
    queryset = AuditLog.objects.select_related("actor").all()


class PlatformAuditListView(ListAPIView):
    """Platform staff's own actions (RLS lets the platform see rows flagged via_platform plus platform-level rows)."""

    permission_classes = [IsPlatformAdmin]
    serializer_class = PlatformAuditSerializer
    queryset = AuditLog.objects.select_related("actor", "tenant").all()
