from django.urls import path

from . import views

urlpatterns = [
    path("shifts/prefill/", views.PrefillView.as_view()),
    path("shifts/start/", views.ShiftStartView.as_view()),
    path("shifts/", views.ShiftListView.as_view()),
    path("shifts/<str:pk>/", views.ShiftDetailView.as_view()),
    path("shifts/<str:pk>/end/", views.ShiftEndView.as_view()),
    path("shifts/<str:pk>/checkpoint/", views.ShiftCheckpointView.as_view()),
    path("shifts/<str:pk>/review/", views.ShiftReviewView.as_view()),
    path("tank-readings/", views.TankDipView.as_view()),
    path("deliveries/", views.DeliveryView.as_view()),
    path("readings/<str:kind>/<str:pk>/photo/", views.ReadingPhotoView.as_view()),
    path("qr/<str:token>/", views.QRResolveView.as_view()),
    path("qr-image/<str:kind>/<str:pk>/", views.QRImageView.as_view()),
    path("media/<str:model>/<str:pk>/<str:field>/", views.MediaView.as_view()),
    path("sync/", views.SyncView.as_view()),
    path("sync/rejected/", views.SyncRejectedView.as_view()),
]
