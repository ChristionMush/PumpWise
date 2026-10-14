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
    path("shifts/<str:pk>/join/", views.ShiftJoinView.as_view()),
    path("shifts/<str:pk>/leave/", views.ShiftLeaveView.as_view()),
    path("shifts/<str:pk>/cash/", views.ShiftCashView.as_view()),
    path("shifts/<str:pk>/close/", views.ShiftCloseView.as_view()),
    path("shifts/<str:pk>/reassign/", views.ShiftReassignView.as_view()),
    path("shifts/<str:pk>/readings/resolve/", views.ShiftResolveReadingView.as_view()),
    path("tank-readings/", views.TankDipView.as_view()),
    path("deliveries/", views.DeliveryView.as_view()),
    path("readings/<str:kind>/<str:pk>/photo/", views.ReadingPhotoView.as_view()),
    path("media/<str:model>/<str:pk>/<str:field>/", views.MediaView.as_view()),
    path("sync/", views.SyncView.as_view()),
    path("sync/rejected/", views.SyncRejectedView.as_view()),
]
