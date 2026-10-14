from django.urls import path

from .views import (
    MySubscriptionView,
    PlatformConvertTrialView,
    PlatformFeaturesView,
    PlatformPumpSystemView,
    PlatformStationPolicyView,
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
    path("platform/tenants/<str:pk>/", PlatformTenantDetailView.as_view(), name="platform-tenant"),
    path("platform/tenants/<str:pk>/stations/", PlatformStationView.as_view(), name="platform-tenant-stations"),
    path("platform/trials/", PlatformTrialView.as_view(), name="platform-trials"),
    path("platform/tenants/<str:pk>/convert-trial/", PlatformConvertTrialView.as_view(), name="platform-convert-trial"),
    path("platform/tenants/<str:pk>/features/", PlatformFeaturesView.as_view(), name="platform-tenant-features"),
    path("platform/tenants/<str:pk>/pump-system/", PlatformPumpSystemView.as_view(), name="platform-tenant-pump-system"),
    path("platform/tenants/<str:pk>/stations/<str:station_id>/policy/", PlatformStationPolicyView.as_view(), name="platform-station-policy"),
    path("platform/tenants/<str:pk>/subscription/", PlatformSubscriptionView.as_view(), name="platform-tenant-subscription"),
    path("platform/tenants/<str:pk>/reset-owner-password/", ResetOwnerPasswordView.as_view(), name="platform-reset-owner"),
    path("subscription/", MySubscriptionView.as_view(), name="my-subscription"),
    path("settings/", TenantSettingsView.as_view(), name="tenant-settings"),
    path("settings/logo/", TenantLogoView.as_view(), name="tenant-logo"),
]
