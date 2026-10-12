from django.urls import path

from . import views

urlpatterns = [
    path("billing/webhook/<str:provider>/", views.WebhookView.as_view()),
    path("billing/status/", views.BillingStatusView.as_view()),
    path("billing/checkout/", views.CheckoutView.as_view()),
    path("platform/billing/plans/", views.PlanListView.as_view()),
    path("platform/billing/plans/<str:pk>/", views.PlanDetailView.as_view()),
    path("platform/billing/quote/", views.QuoteView.as_view()),
    path("platform/billing/resellers/", views.ResellerListView.as_view()),
    path("platform/billing/payments/", views.PlatformPaymentListView.as_view()),
]
