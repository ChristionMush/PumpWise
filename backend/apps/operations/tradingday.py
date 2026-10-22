"""The trading day: 21:00 to 21:00, named by the day it closes (21:00 on 21 Oct to 21:00 on 22 Oct is 22 Oct).

Each station's day ends when its manager closes it (DayClose): the pumps pause, every tank is dipped and every pump's
meter is read, a little after 21:00 as people take time. Until a day is closed, 21:00 stands in. Every daily figure in
the system (sales, the fuel check, the dashboard's "today", reports, the accounting pack) uses these bounds.
"""
from datetime import date, datetime, time, timedelta

from django.utils import timezone

CLOSES_AT = time(21, 0)


def nominal_close(day: date):
    """21:00 local on the day: when a trading day ends if nobody closed it."""
    return timezone.make_aware(datetime.combine(day, CLOSES_AT), timezone.get_current_timezone())


def day_of_close(at):
    """The trading day a close at this moment ends: around 21:00 closes that day; one after midnight still closes the
    day before (a very late close)."""
    return (timezone.localtime(at) - timedelta(hours=12)).date()


class Calendar:
    """One station's trading days. Loads the day closes once, so many lookups stay cheap. station_id=None: 21:00."""

    def __init__(self, station_id=None):
        self.station_id = station_id
        self._closes = None

    def _load(self):
        if self._closes is None:
            from .models import DayClose

            self._closes = {} if self.station_id is None else dict(DayClose.objects.filter(station_id=self.station_id).values_list("day", "closed_at"))
        return self._closes

    def close_of(self, day: date):
        return self._load().get(day) or nominal_close(day)

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
