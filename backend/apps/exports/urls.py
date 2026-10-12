from django.urls import path

from . import views

urlpatterns = [
    path("exports/", views.ExportListView.as_view()),
    path("exports/download/", views.ExportDownloadView.as_view()),
    path("exports/<str:pk>/link/", views.ExportLinkView.as_view()),
]
