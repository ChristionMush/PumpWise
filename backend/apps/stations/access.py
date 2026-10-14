from apps.accounts.permissions import sees_all_stations

from .models import StationAssignment


def visible_station_ids(user):
    """None = all stations of the tenant. Owners see all; a manager/attendant with assignments sees only those."""
    if sees_all_stations(user):
        return None
    ids = list(StationAssignment.objects.filter(user_id=user.pk).values_list("station_id", flat=True))
    return ids or None


def scope(qs, user, field="station_id"):
    ids = visible_station_ids(user)
    return qs if ids is None else qs.filter(**{f"{field}__in": ids})
