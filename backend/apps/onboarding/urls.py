from django.urls import path

from . import views

urlpatterns = [
    path("onboarding/checklist/", views.ChecklistView.as_view()),
    path("onboarding/flags/", views.FlagView.as_view()),
    path("platform/tenants/<str:pk>/onboarding/", views.PlatformChecklistView.as_view()),
]
