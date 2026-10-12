from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.http import Http404, HttpResponse
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsOwner
from apps.audit import services as audit
from apps.core.db import set_db_context

from .models import TenantExport
from .storage import get_storage, read_token
from .tasks import build_export


class ExportSerializer(serializers.ModelSerializer):
    class Meta:
        model = TenantExport
        fields = ["id", "kind", "status", "size_bytes", "row_counts", "expires_at", "error", "created_at"]


class ExportListView(APIView):
    """Owner: 'Export my data'. The file is prepared in the background; ask for a link when it is ready."""

    permission_classes = [IsOwner]

    def get(self, request):
        return Response(ExportSerializer(TenantExport.objects.all()[:20], many=True).data)

    def post(self, request):
        if TenantExport.objects.filter(status="pending", created_at__gte=timezone.now() - timedelta(minutes=30)).exists():
            raise ValidationError({"detail": "An export is already being prepared. Please wait a few minutes."})
        export = TenantExport.objects.create(tenant_id=request.user.tenant_id, requested_by=request.user)
        audit.log("export_requested", actor=request.user, obj=export, request=request)
        # Queued for the worker; (eager in tests/dev without a broker) it runs after this request's data is committed.
        from django.db import transaction

        transaction.on_commit(lambda: build_export.delay(str(request.user.tenant_id), str(export.pk)))
        return Response(ExportSerializer(export).data, status=status.HTTP_202_ACCEPTED)


class ExportLinkView(APIView):
    permission_classes = [IsOwner]

    def post(self, request, pk):
        export = TenantExport.objects.filter(pk=pk).first()
        if export is None:
            raise NotFound()
        if export.status != "ready" or (export.expires_at and export.expires_at < timezone.now()):
            raise ValidationError({"detail": "This export is not available. Please request a new one."})
        ttl = settings.EXPORT_LINK_TTL_SECONDS
        audit.log("export_link_issued", actor=request.user, obj=export, request=request)
        return Response({"url": get_storage().link(export, ttl), "expires_in_seconds": ttl})


class ExportDownloadView(APIView):
    """Local-storage download for a signed, time-limited link (S3 uses its own presigned URLs)."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            tenant_id, export_id = read_token(request.query_params.get("token", ""), settings.EXPORT_LINK_TTL_SECONDS)
        except (signing.BadSignature, ValueError):
            raise Http404
        set_db_context(tenant_id=tenant_id)  # the signed link carries the tenant; RLS then scopes the lookup
        export = TenantExport.objects.filter(pk=export_id, status="ready").first()
        if export is None or (export.expires_at and export.expires_at < timezone.now()):
            raise Http404
        audit.log("export_downloaded", tenant_id=tenant_id, obj=export, request=request)
        resp = HttpResponse(get_storage().read(export.file_key), content_type="application/zip")
        resp["Content-Disposition"] = 'attachment; filename="my-data.zip"'
        resp["Cache-Control"] = "no-store"
        return resp
