"""
DataSourceAdapter: the seam between where numbers come from and the core records.

Every adapter turns a device feed (or a person typing) into the SAME internal records:
PumpMeterReading / TankReading / Transaction. The core model, reconciliation and dashboard never
know the difference. Dispatch is by the pump's / tank's `data_source` field, so adding an automatic
adapter later (pulse reader, forecourt controller, tank gauge) means writing one class and registering
it. Nothing else changes.

Tier 1 (this phase): `ManualAdapter`. Tier 2 adapters are intentionally NOT implemented.
See docs/device-ingestion.md for the planned ingestion design.
"""
from abc import ABC, abstractmethod

from django.utils import timezone

from .models import PumpMeterReading, TankReading


class AdapterNotAvailable(Exception):
    """The pump/tank is set to a data source that has no adapter yet."""


class DataSourceAdapter(ABC):
    source = ""

    @abstractmethod
    def record_meter_reading(self, *, shift, nozzle, kind, reading, at, user, photo=None) -> PumpMeterReading: ...

    @abstractmethod
    def record_tank_reading(self, *, tank, litres, dip_mm, at, user, shift=None, photo=None, client_id=None) -> TankReading: ...


class ManualAdapter(DataSourceAdapter):
    """An attendant typed (or photographed) the number."""

    source = "manual"

    def record_meter_reading(self, *, shift, nozzle, kind, reading, at, user, photo=None):
        at = at or timezone.now()
        existing = None
        if kind != PumpMeterReading.Kind.CHECKPOINT:
            existing = PumpMeterReading.objects.filter(shift=shift, nozzle=nozzle, kind=kind).first()
        if existing:
            if existing.reading != reading:
                # A correction: keep the row, mark it, and let the audit log carry the old value.
                existing.reading, existing.reading_at, existing.corrected = reading, at, True
                existing.save(update_fields=["reading", "reading_at", "corrected", "updated_at"])
            return existing
        return PumpMeterReading.objects.create(
            tenant_id=shift.tenant_id, shift=shift, nozzle=nozzle, kind=kind, reading=reading, reading_at=at,
            source=self.source, recorded_by=user, photo=photo,
        )

    def record_tank_reading(self, *, tank, litres, dip_mm, at, user, shift=None, photo=None, client_id=None):
        return TankReading.objects.create(
            tenant_id=tank.tenant_id, client_id=client_id, tank=tank, shift=shift, dip_mm=dip_mm, litres=litres,
            reading_at=at or timezone.now(), source=self.source, recorded_by=user, photo=photo,
        )


_REGISTRY: dict[str, DataSourceAdapter] = {"manual": ManualAdapter()}


def register(data_source, adapter):
    _REGISTRY[data_source] = adapter


def get_adapter(data_source) -> DataSourceAdapter:
    try:
        return _REGISTRY[data_source]
    except KeyError:
        raise AdapterNotAvailable(f"Automatic data source '{data_source}' is not enabled yet.")
