from django.urls import include, path

from apps.core.views import health

urlpatterns = [
    path("health/", health),
    path("api/", include("apps.accounts.urls")),
    path("api/", include("apps.tenants.urls")),
    path("api/", include("apps.stations.urls")),
    path("api/", include("apps.operations.urls")),
    path("api/", include("apps.reports.urls")),
    path("api/", include("apps.insights.urls")),
    path("api/", include("apps.billing.urls")),
    path("api/", include("apps.exports.urls")),
    path("api/", include("apps.onboarding.urls")),
    path("api/", include("apps.audit.urls")),
]
