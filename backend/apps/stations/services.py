"""Pricing and calibration rules."""
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import ExchangeRate, FuelPrice


def price_at(station_id, fuel_type_id, currency, at):
    """The price in force at `at` (None if none). This is what every sale uses."""
    return (
        FuelPrice.objects.filter(station_id=station_id, fuel_type_id=fuel_type_id, currency=currency, effective_from__lte=at)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=at))
        .order_by("-effective_from")
        .first()
    )


def set_price(*, tenant_id, station, fuel_type, currency, price, effective_from=None, user=None):
    """Add a new price. It closes the previous open price at the same instant, so periods never overlap."""
    effective_from = effective_from or timezone.now()
    with transaction.atomic():
        series = FuelPrice.objects.select_for_update().filter(station=station, fuel_type=fuel_type, currency=currency)
        latest = series.order_by("-effective_from").first()
        if latest and effective_from <= latest.effective_from:
            raise ValidationError({"effective_from": "A new price must start after the current price starts."})
        open_price = series.filter(effective_to__isnull=True).first()
        if open_price:
            open_price.effective_to = effective_from
            open_price.save(update_fields=["effective_to", "updated_at"])
        return FuelPrice.objects.create(
            tenant_id=tenant_id, station=station, fuel_type=fuel_type, currency=currency,
            price_per_litre=price, effective_from=effective_from, created_by=user,
        )


def rate_at(from_currency, to_currency, at):
    """Conversion factor from -> to using the owner's rates (direct, else inverse). None if no rate set."""
    if from_currency == to_currency:
        return Decimal(1)
    direct = ExchangeRate.objects.filter(from_currency=from_currency, to_currency=to_currency, effective_from__lte=at).first()
    inverse = ExchangeRate.objects.filter(from_currency=to_currency, to_currency=from_currency, effective_from__lte=at).first()
    if direct and (not inverse or direct.effective_from >= inverse.effective_from):
        return direct.rate
    if inverse:
        return Decimal(1) / inverse.rate
    return None


def litres_from_dip(tank, dip_mm):
    """Linear interpolation on the tank's calibration chart. Raises if the chart cannot answer."""
    points = list(tank.calibration.order_by("dip_mm").values_list("dip_mm", "litres"))
    if len(points) < 2:
        raise ValidationError({"dip_mm": "This tank has no calibration chart yet. Enter litres instead."})
    dip = Decimal(str(dip_mm))
    if dip < points[0][0] or dip > points[-1][0]:
        raise ValidationError({"dip_mm": "Dip height is outside the calibration chart."})
    for (d0, l0), (d1, l1) in zip(points, points[1:], strict=False):
        if d0 <= dip <= d1:
            return (l0 + (l1 - l0) * (dip - d0) / (d1 - d0)).quantize(Decimal("0.001"))
    raise ValidationError({"dip_mm": "Dip height is outside the calibration chart."})
