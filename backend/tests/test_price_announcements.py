"""A new price is announced on everyone's home screen; only the owner is told who set it and when."""
from datetime import datetime, timedelta

from django.utils import timezone

from apps.accounts.models import Role
from apps.core.db import db_context
from apps.stations.models import StationAssignment

from .helpers import client_for, make_forecourt, two_tenants


def _set(user, fc, price, **extra):
    r = client_for(user).post("/api/prices/", {"station": str(fc.station.pk), "fuel_type": str(fc.fuel.pk), "price_per_litre": price, **extra}, format="json")
    assert r.status_code == 201, r.content
    return r.json()


def _news(user):
    r = client_for(user).get("/api/prices/announcements/")
    assert r.status_code == 200
    return r.json()["announcements"]


def test_everyone_sees_a_new_price_and_only_the_owner_sees_who_set_it():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")  # started 60 days ago: old news, not announced
    assert _news(a[Role.OWNER]) == []
    _set(a[Role.MANAGER], fc, "1.70")

    for role in (Role.MANAGER, Role.ATTENDANT):
        [item] = _news(a[role])
        assert item["price_per_litre"] == "1.7000" and item["previous_price"] == "1.5000" and item["fuel"] == "Petrol"
        assert item["station_name"] == fc.station.name and item["upcoming"] is False
        assert not {"set_by", "set_by_role", "set_at"} & item.keys()

    [item] = _news(a[Role.OWNER])
    assert item["set_by"] == a[Role.MANAGER].full_name and item["set_by_role"] == Role.MANAGER
    assert abs(datetime.fromisoformat(item["set_at"]) - timezone.now()) < timedelta(minutes=1)


def test_price_history_tells_only_the_owner_who_set_each_price():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    _set(a[Role.MANAGER], fc, "1.70")
    owner = client_for(a[Role.OWNER]).get("/api/prices/").json()
    assert [p["set_by"] for p in owner] == [a[Role.MANAGER].full_name, None]  # the seeded price has no setter
    for role in (Role.MANAGER, Role.ATTENDANT):
        assert all("set_by" not in p for p in client_for(a[role]).get("/api/prices/").json())


def test_upcoming_prices_are_announced_and_replaced_or_old_ones_are_not():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    _set(a[Role.OWNER], fc, "1.60", effective_from=(timezone.now() - timedelta(hours=72)).isoformat())  # too old
    _set(a[Role.OWNER], fc, "1.65", effective_from=(timezone.now() - timedelta(hours=1)).isoformat())
    _set(a[Role.OWNER], fc, "1.80", effective_from=(timezone.now() + timedelta(hours=6)).isoformat())
    news = _news(a[Role.ATTENDANT])
    assert [(n["price_per_litre"], n["upcoming"]) for n in news] == [("1.8000", True), ("1.6500", False)]
    assert news[0]["previous_price"] == "1.6500"


def test_announcements_follow_station_assignment_and_tenant():
    a, b = two_tenants()
    mine, other = make_forecourt(a["tenant"], name="Mine"), make_forecourt(a["tenant"], name="Other", code="DIESEL")
    with db_context(tenant_id=a["tenant"].id):
        StationAssignment.objects.create(tenant_id=a["tenant"].id, user=a[Role.ATTENDANT], station=mine.station)
    _set(a[Role.OWNER], mine, "1.70")
    _set(a[Role.OWNER], other, "1.90")
    assert [n["station_name"] for n in _news(a[Role.ATTENDANT])] == ["Mine"]
    assert len(_news(a[Role.OWNER])) == 2
    for role in (Role.OWNER, Role.MANAGER, Role.ATTENDANT):
        assert _news(b[role]) == []
