"""Fuel pools: a nozzle can draw from more than one tank of its fuel (its tank, and the tanks added under "also draws
from", NozzleTank). Tanks joined that way are one stock for the fuel check, the forecast and the alerts: their dips are
added up and their sales are those of every nozzle drawing from them. A tank no nozzle shares (every Fuel Zone and
Litrix tank) is a pool of itself, with its name, id and figures, so nothing changes there.

A pool behaves like a tank where the figures are worked out: its dip at a moment is the sum of each tank's latest dip
up to then, its deliveries are those into any of its tanks, and its sales are those of the nozzles drawing from it.
"""
from decimal import Decimal
from types import SimpleNamespace

from django.db.models import Sum


class Pool:
    def __init__(self, tanks, nozzle_ids):
        self.tanks = sorted(tanks, key=lambda t: t.name.lower())
        self.nozzle_ids = list(nozzle_ids)  # every nozzle drawing from any of its tanks
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

        return Transaction.objects.filter(nozzle_id__in=self.nozzle_ids)

    def sold(self, after, until):
        return self.sales().filter(occurred_at__gt=after, occurred_at__lte=until).aggregate(s=Sum("litres"))["s"] or Decimal(0)


def nozzle_tanks(nozzle):
    """The tanks a nozzle draws from: its tank, then the others it also draws from."""
    from .models import NozzleTank

    return [nozzle.tank_id, *NozzleTank.objects.filter(nozzle=nozzle).exclude(tank_id=nozzle.tank_id).values_list("tank_id", flat=True)]


def prune_links(nozzle):
    """After a nozzle's tank changed: drop "also draws from" links to that tank itself or to another fuel's tanks (a
    petrol nozzle moved to a diesel tank must not join petrol and diesel into one stock)."""
    from .models import NozzleTank

    for link in NozzleTank.objects.filter(nozzle=nozzle).select_related("tank"):
        if link.tank_id == nozzle.tank_id or link.tank.fuel_type_id != nozzle.tank.fuel_type_id:
            link.delete()


def _component(station_id, tank_id):
    """(tank ids, nozzle ids) joined to this tank through nozzles drawing from more than one tank."""
    from .models import Nozzle, NozzleTank

    draws = {n: {t} for n, t in Nozzle.objects.filter(pump__station_id=station_id).values_list("pk", "tank_id")}
    for n, t in NozzleTank.objects.filter(nozzle_id__in=draws).values_list("nozzle_id", "tank_id"):
        draws[n].add(t)
    tanks, nozzles, grew = {tank_id}, set(), True
    while grew:
        grew = False
        for n, ts in draws.items():
            if n not in nozzles and ts & tanks:
                nozzles.add(n)
                if not ts <= tanks:
                    tanks |= ts
                    grew = True
    return tanks, nozzles


def pool_of(tank):
    """The pool a tank belongs to: every tank joined to it through a nozzle drawing from several tanks."""
    from .models import Tank

    if isinstance(tank, Pool):
        return tank
    tank_ids, nozzle_ids = _component(tank.station_id, tank.pk)
    tanks = [tank] if tank_ids == {tank.pk} else list(Tank.objects.filter(pk__in=tank_ids).select_related("fuel_type", "station"))
    return Pool(tanks, nozzle_ids)


def pools_for(tanks):
    """The pools of these tanks, once each (two joined tanks given twice are one pool), in the tanks' order."""
    seen, out = set(), []
    for t in tanks:
        if t.pk in seen:
            continue
        p = pool_of(t)
        seen.update(p.ids)
        out.append(p)
    return out
