"""Fuel pools: every pump of a fuel draws from every tank of that fuel at its station (e.g. both petrol tanks), so for
stock, sales and the fuel check the tanks of one fuel are one pool. A fuel with one tank (most stations) is a pool of
that tank, with its name, id and figures, so nothing changes there. A business with Tenant.pooled_tanks off (Fuel Zone,
Litrix) keeps every tank its own pool, fed by the pumps whose nozzles are on it, as before pools existed.

A pool behaves like a tank where the figures are worked out: its dip at a moment is the sum of each tank's latest dip
up to then, its deliveries are those into any of its tanks, and its sales are every sale of its fuel at the station.
"""
from decimal import Decimal
from types import SimpleNamespace

from django.db.models import Sum


class Pool:
    def __init__(self, tanks, pooled=True):
        self.pooled = pooled  # False: one tank, its sales those of the nozzles on it
        self.tanks = sorted(tanks, key=lambda t: t.name.lower())
        self.lead = self.tanks[0]  # its id names the pool (its status, its alerts)
        self.pk, self.tenant_id, self.station_id = self.lead.pk, self.lead.tenant_id, self.lead.station_id
        self.station, self.fuel_type, self.fuel_type_id = self.lead.station, self.lead.fuel_type, self.lead.fuel_type_id
        self.name = " + ".join(t.name for t in self.tanks)
        self.capacity_litres = sum((t.capacity_litres for t in self.tanks), Decimal(0))
        self.reserve_litres = sum((t.reserve_litres for t in self.tanks), Decimal(0))

    @property
    def ids(self):
        return [t.pk for t in self.tanks]

    def _snapshot(self, rows):
        """The pool's level from one dip per tank (None if a tank was never dipped)."""
        if not rows or any(r is None for r in rows):
            return None
        last = max(rows, key=lambda r: (r.reading_at, str(r.pk)))
        return SimpleNamespace(pk=last.pk, reading_at=last.reading_at, litres=sum((r.litres for r in rows), Decimal(0)))

    def dip_at(self, at):
        """The level at that moment: each tank's latest dip up to then, added up."""
        from apps.operations.models import TankReading

        return self._snapshot([TankReading.objects.filter(tank=t, reading_at__lte=at).order_by("-reading_at").first() for t in self.tanks])

    def dip_before(self, at):
        from apps.operations.models import TankReading

        return self._snapshot([TankReading.objects.filter(tank=t, reading_at__lt=at).order_by("-reading_at").first() for t in self.tanks])

    def first_dip_in(self, start, end):
        """The first moment in (start, end] by which every tank of the pool has been dipped, and the level then."""
        from apps.operations.models import TankReading

        firsts = [TankReading.objects.filter(tank=t, reading_at__gt=start, reading_at__lte=end).order_by("reading_at").first() for t in self.tanks]
        if any(f is None for f in firsts):
            return None
        return self.dip_at(max(f.reading_at for f in firsts))

    def dips_since(self, since):
        """The pool's level at each dip taken since then (a dip of any of its tanks), in order."""
        from apps.operations.models import TankReading

        moments = sorted(set(TankReading.objects.filter(tank__in=self.ids, reading_at__gte=since).values_list("reading_at", flat=True)))
        return [s for s in (self.dip_at(m) for m in moments) if s is not None]

    def delivered(self, after, until):
        from apps.operations.models import Delivery

        return Delivery.objects.filter(tank__in=self.ids, delivered_at__gt=after, delivered_at__lte=until).aggregate(s=Sum("litres_received"))["s"] or Decimal(0)

    def sales(self):
        from apps.operations.models import Transaction

        if not self.pooled:
            return Transaction.objects.filter(nozzle__tank__in=self.ids)
        return Transaction.objects.filter(station_id=self.station_id, fuel_type_id=self.fuel_type_id)

    def sold(self, after, until):
        return self.sales().filter(occurred_at__gt=after, occurred_at__lte=until).aggregate(s=Sum("litres"))["s"] or Decimal(0)


def pooled(tenant_id):
    from apps.tenants.models import Tenant

    return Tenant.objects.filter(pk=tenant_id, pooled_tanks=True).exists()


def pool_of(tank):
    """The pool a tank belongs to: every tank of its fuel at its station (or the tank alone, pooled_tanks off)."""
    from .models import Tank

    if isinstance(tank, Pool):
        return tank
    if not pooled(tank.tenant_id):
        return Pool([tank], pooled=False)
    return Pool(list(Tank.objects.filter(station_id=tank.station_id, fuel_type_id=tank.fuel_type_id).select_related("fuel_type", "station")))


def pools_for(tanks):
    """The pools of these tanks, once each (a fuel with two tanks given twice is one pool), in the tanks' order."""
    seen, out = set(), []
    for t in tanks:
        key = (t.station_id, t.fuel_type_id) if pooled(t.tenant_id) else t.pk
        if key not in seen:
            seen.add(key)
            out.append(pool_of(t))
    return out
