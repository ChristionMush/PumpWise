from django.urls import path

from . import views

urlpatterns = [
    path("dashboard/", views.DashboardView.as_view()),
    path("forecast/", views.ForecastView.as_view()),
    path("alerts/", views.AlertListView.as_view()),
    path("alerts/refresh/", views.AlertRefreshView.as_view()),
    path("alerts/summary/", views.AlertSummaryView.as_view()),
    path("alerts/<str:pk>/", views.AlertDetailView.as_view()),
    path("alerts/<str:pk>/resolve/", views.AlertResolveView.as_view()),
    path("alerts/<str:pk>/approve/", views.AlertApproveView.as_view()),
    path("alerts/<str:pk>/accept/", views.AlertAcceptView.as_view()),
    path("alerts/<str:pk>/send-back/", views.AlertSendBackView.as_view()),
    path("summary/", views.SummaryPreviewView.as_view()),
    path("messages/", views.MessageLogView.as_view()),
]
