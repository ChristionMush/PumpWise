from django.urls import path

from .views import AuditLogListView, PlatformAuditListView

urlpatterns = [
    path("audit/", AuditLogListView.as_view(), name="audit-log"),
    path("platform/audit/", PlatformAuditListView.as_view(), name="platform-audit"),
]
