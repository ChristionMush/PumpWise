"""The trading day: 21:00 to 21:00, named by the day it closes (21:00 on 21 Oct to 21:00 on 22 Oct is 22 Oct).

Each station's day ends when its manager closes it (DayClose): the pumps pause, every tank is dipped and every pump's
meter is read, a little after 21:00 as people take time. Until a day is closed, 21:00 stands in; a business that closes
the day at a handover (module "day_close") has that handover stand in instead. Every daily figure in the
system (sales, the fuel check, the dashboard's "today", reports, the accounting pack) uses these bounds.
"""
from datetime import date, datetime, time, timedelta

from django.utils import timezone

CLOSES_AT = time(21, 0)


def nominal_close(day: date, change=""):
    """When a trading day ends if nobody closed it: 21:00 local on the day, or the business's closing shift change
    ("18:00" on the day; a morning change, "07:00", the morning after, as a close then still ends this day)."""
    if not change:
        return timezone.make_aware(datetime.combine(day, CLOSES_AT), timezone.get_current_timezone())
    hh, mm = map(int, change.split(":"))
    on = day if hh >= 12 else day + timedelta(days=1)
    return timezone.make_aware(datetime.combine(on, time(hh, mm)), timezone.get_current_timezone())


def day_of_close(at):
    """The trading day a close at this moment ends: around 21:00 closes that day; one after midnight still closes the
    day before (a very late close)."""
    return (timezone.localtime(at) - timedelta(hours=12)).date()


class Calendar:
    """One station's trading days. Loads the day closes once, so many lookups stay cheap. station_id=None: 21:00."""

    def __init__(self, station_id=None):
        self.station_id = station_id
        self._closes = None
        self._change = ""

    def _load(self):
        if self._closes is None:
            from apps.stations.models import Station

            from .models import DayClose

            self._closes = {} if self.station_id is None else dict(DayClose.objects.filter(station_id=self.station_id).values_list("day", "closed_at"))
            if self.station_id is not None:
                from apps.tenants import modules

                station = Station.all_objects.filter(pk=self.station_id).select_related("tenant").first()
                self._change = modules.handover(modules.of(station.tenant)) if station else ""
        return self._closes

    @property
    def closes_at_change(self):
        """The business's closing shift change ("18:00", "07:00"), or "" (closed on the Dip screen, about 21:00)."""
        self._load()
        return self._change

    def close_of(self, day: date):
        return self._load().get(day) or nominal_close(day, self._change)

    def bounds(self, day: date):
        """(start, end] of a trading day: after the previous day's close, up to and including this day's close (sales
        closed off by the readings taken at the close belong to the day being closed)."""
        return self.close_of(day - timedelta(days=1)), self.close_of(day)

    def day_of(self, at):
        """The trading day a moment belongs to."""
        d = timezone.localtime(at).date()
        if at <= self.close_of(d - timedelta(days=1)):  # up to a late close of the day before
            return d - timedelta(days=1)
        return d if at <= self.close_of(d) else d + timedelta(days=1)

    def today(self):
        return self.day_of(timezone.now())


def bounds(station_id, day: date):
    return Calendar(station_id).bounds(day)


def day_of(station_id, at):
    return Calendar(station_id).day_of(at)


def today(station_id=None):
    return Calendar(station_id).today()


def latest_today(station_ids=None):
    """Today for a choice of dates: 21:00 to 21:00, or later where a business that closes at a shift change has
    already closed (from its handover on, its next day is today)."""
    from apps.stations.models import Station
    from apps.tenants import modules

    stations = Station.objects.filter(modules.closing_at_a_handover("tenant__"))
    if station_ids is not None:
        stations = stations.filter(pk__in=station_ids)
    return max([today(), *(today(pk) for pk in stations.values_list("pk", flat=True))])
