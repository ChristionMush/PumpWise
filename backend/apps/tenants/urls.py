from django.urls import path

from .views import (
    MySubscriptionView,
    PlatformStationView,
    PlatformSubscriptionView,
    PlatformTenantDetailView,
    PlatformTenantListCreateView,
    ResetOwnerPasswordView,
    TenantLogoView,
    TenantSettingsView,
)

urlpatterns = [
    path("platform/tenants/", PlatformTenantListCreateView.as_view(), name="platform-tenants"),
    path("platform/tenants/<str:pk>/", PlatformTenantDetailView.as_view(), name="platform-tenant"),
    path("platform/tenants/<str:pk>/stations/", PlatformStationView.as_view(), name="platform-tenant-stations"),
    path("platform/tenants/<str:pk>/subscription/", PlatformSubscriptionView.as_view(), name="platform-tenant-subscription"),
    path("platform/tenants/<str:pk>/reset-owner-password/", ResetOwnerPasswordView.as_view(), name="platform-reset-owner"),
    path("subscription/", MySubscriptionView.as_view(), name="my-subscription"),
    path("settings/", TenantSettingsView.as_view(), name="tenant-settings"),
    path("settings/logo/", TenantLogoView.as_view(), name="tenant-logo"),
]
