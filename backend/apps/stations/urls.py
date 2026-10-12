from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("stations", views.StationViewSet, basename="station")
router.register("fuel-types", views.FuelTypeViewSet, basename="fueltype")
router.register("tanks", views.TankViewSet, basename="tank")
router.register("pumps", views.PumpViewSet, basename="pump")
router.register("nozzles", views.NozzleViewSet, basename="nozzle")
router.register("exchange-rates", views.ExchangeRateViewSet, basename="exchangerate")

urlpatterns = [
    path("prices/", views.FuelPriceView.as_view(), name="prices"),
    path("users/<str:pk>/stations/", views.StationAssignmentView.as_view(), name="user-stations"),
    path("", include(router.urls)),
]
