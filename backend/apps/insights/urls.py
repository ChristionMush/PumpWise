from django.urls import path

from . import views

urlpatterns = [
    path("dashboard/", views.DashboardView.as_view()),
    path("forecast/", views.ForecastView.as_view()),
    path("alerts/", views.AlertListView.as_view()),
    path("alerts/refresh/", views.AlertRefreshView.as_view()),
    path("alerts/<str:pk>/resolve/", views.AlertResolveView.as_view()),
    path("summary/", views.SummaryPreviewView.as_view()),
    path("messages/", views.MessageLogView.as_view()),
]
