from django.urls import path

from . import views

urlpatterns = [
    path("reports/daily-sales/", views.DailySalesView.as_view()),
    path("reconciliation/tanks/", views.TankReconciliationView.as_view()),
    path("reconciliation/cash/", views.CashReconciliationView.as_view()),
    path("reports/attendants/", views.AttendantReportView.as_view()),
]
