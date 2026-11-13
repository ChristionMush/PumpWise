from django.urls import path

from .mark_views import PlatformMarkDetailView, PlatformMarkListView, PlatformModulesView, PlatformTenantMarkView, PlatformTenantModulesView
from .views import (
    MySubscriptionView,
    PlatformConvertTrialView,
    PlatformFeaturesView,
    PlatformNozzleTankView,
    PlatformPumpSystemView,
    PlatformStationPolicyView,
    PlatformStationTanksView,
    PlatformStationView,
    PlatformSubscriptionView,
    PlatformTenantDetailView,
    PlatformTenantListCreateView,
    PlatformTrialView,
    ResetOwnerPasswordView,
    TenantLogoView,
    TenantSettingsView,
)

urlpatterns = [
    path("platform/tenants/", PlatformTenantListCreateView.as_view(), name="platform-tenants"),
    path("platform/modules/", PlatformModulesView.as_view(), name="platform-modules"),
    path("platform/marks/", PlatformMarkListView.as_view(), name="platform-marks"),
    path("platform/marks/<str:pk>/", PlatformMarkDetailView.as_view(), name="platform-mark"),
    path("platform/tenants/<str:pk>/mark/", PlatformTenantMarkView.as_view(), name="platform-tenant-mark"),
    path("platform/tenants/<str:pk>/modules/", PlatformTenantModulesView.as_view(), name="platform-tenant-modules"),
    path("platform/tenants/<str:pk>/", PlatformTenantDetailView.as_view(), name="platform-tenant"),
    path("platform/tenants/<str:pk>/stations/", PlatformStationView.as_view(), name="platform-tenant-stations"),
    path("platform/trials/", PlatformTrialView.as_view(), name="platform-trials"),
    path("platform/tenants/<str:pk>/convert-trial/", PlatformConvertTrialView.as_view(), name="platform-convert-trial"),
    path("platform/tenants/<str:pk>/features/", PlatformFeaturesView.as_view(), name="platform-tenant-features"),
    path("platform/tenants/<str:pk>/pump-system/", PlatformPumpSystemView.as_view(), name="platform-tenant-pump-system"),
    path("platform/tenants/<str:pk>/stations/<str:station_id>/policy/", PlatformStationPolicyView.as_view(), name="platform-station-policy"),
    path("platform/tenants/<str:pk>/stations/<str:station_id>/tanks/", PlatformStationTanksView.as_view(), name="platform-station-tanks"),
    path("platform/tenants/<str:pk>/nozzles/<str:nozzle_id>/tank/", PlatformNozzleTankView.as_view(), name="platform-nozzle-tank"),
    path("platform/tenants/<str:pk>/subscription/", PlatformSubscriptionView.as_view(), name="platform-tenant-subscription"),
    path("platform/tenants/<str:pk>/reset-owner-password/", ResetOwnerPasswordView.as_view(), name="platform-reset-owner"),
    path("subscription/", MySubscriptionView.as_view(), name="my-subscription"),
    path("settings/", TenantSettingsView.as_view(), name="tenant-settings"),
    path("settings/logo/", TenantLogoView.as_view(), name="tenant-logo"),
]
