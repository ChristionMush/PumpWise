"""A delivery nobody recorded: the "Delivery check" alert carries a delivery form filled from the dips (litres, fuel,
tank, a time between the dips), and saving it records the delivery and closes the alert. Supplier, value and the
delivery note number can be left for later and added from the delivery list. Every business, both Marks."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.accounts.models import Role
from apps.audit.models import AuditLog
from apps.core.db import db_context
from apps.insights.alerts import evaluate_tenant
from apps.insights.models import Alert
from apps.operations.models import Delivery

from .helpers import client_for, dip, make_forecourt, two_tenants


def missing_delivery():
    """6000 L came in between two dips and nobody recorded a delivery."""
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    now = timezone.now()
    first, second = now - timedelta(hours=10), now - timedelta(hours=6)
    dip(a["tenant"], a[Role.OWNER], fc, 2000, first)
    dip(a["tenant"], a[Role.OWNER], fc, 8000, second)
    with db_context(tenant_id=a["tenant"].id):
        evaluate_tenant(a["tenant"].id)
    mgr = client_for(a[Role.MANAGER])
    alert = next(x for x in mgr.get("/api/alerts/").json()["results"] if x["kind"] == "delivery")
    return a, fc, mgr, alert, first, second


def test_the_alert_carries_a_delivery_form_filled_from_the_dips():
    a, fc, mgr, alert, first, second = missing_delivery()
    form = alert["delivery_form"]
    assert form["litres"] == "6000" and form["fuel"] == "Petrol" and form["currency"] == "USD"
    assert form["tank"] == str(fc.tank.pk) and form["tanks"] == [{"id": str(fc.tank.pk), "name": fc.tank.name}]
    start, end = (timezone.datetime.fromisoformat(form[k].replace("Z", "+00:00")) for k in ("from", "to"))
    at = timezone.datetime.fromisoformat(form["delivered_at"].replace("Z", "+00:00"))
    assert abs(start - first) < timedelta(seconds=1) and abs(end - second) < timedelta(seconds=1) and start < at <= end


def test_saving_the_form_records_the_delivery_and_closes_the_alert_with_details_left_for_later():
    a, fc, mgr, alert, first, second = missing_delivery()
    form = alert["delivery_form"]
    res = mgr.post(f"/api/alerts/{alert['id']}/record-delivery/", {"tank": form["tank"], "litres": form["litres"], "delivered_at": form["delivered_at"],
                                                                     "supplier": "", "note_number": "", "value": ""}, format="json")
    assert res.status_code == 200, res.content
    body = res.json()
    assert body["status"] == "resolved" and body["resolution_label"] == "The delivery was recorded" and body["delivery_form"] is None
    assert "6,000 L recorded" in body["resolution_note"] and "to add" in body["resolution_note"]
    with db_context(tenant_id=a["tenant"].id):
        d = Delivery.objects.get()
        assert d.litres_received == Decimal(6000) and first < d.delivered_at <= second and d.recorded_by_id == a[Role.MANAGER].pk
        assert (d.supplier, d.note_number, d.value) == ("", "", None)
        evaluate_tenant(a["tenant"].id)  # the fuel check now balances: nothing comes back
        assert not Alert.objects.filter(kind="delivery", status="open").exists()
        assert AuditLog.objects.filter(action="delivery_recorded").exists()
    pending = mgr.get("/api/deliveries/", {"pending": 1}).json()
    assert [p["id"] for p in pending] == [str(d.pk)] and pending[0]["details_pending"] is True


def test_the_details_are_added_later_and_the_delivery_leaves_the_pending_list():
    a, fc, mgr, alert, *_ = missing_delivery()
    form = alert["delivery_form"]
    mgr.post(f"/api/alerts/{alert['id']}/record-delivery/", {"tank": form["tank"], "litres": form["litres"], "delivered_at": form["delivered_at"]}, format="json")
    did = mgr.get("/api/deliveries/", {"pending": 1}).json()[0]["id"]
    part = mgr.patch(f"/api/deliveries/{did}/", {"supplier": "Kariba Fuel Supplies"}, format="json")
    assert part.status_code == 200 and part.json()["details_pending"] is True  # value and note number still to come
    done = mgr.patch(f"/api/deliveries/{did}/", {"note_number": "KFS-20431", "value": "8460.00", "litres": "1"}, format="json")
    assert done.status_code == 200, done.content
    assert (done.json()["value"], done.json()["currency"], done.json()["details_pending"]) == ("8460.00", "USD", False)
    assert done.json()["litres_received"] == "6000.000"  # litres stay as recorded: the fuel check runs on them
    assert mgr.get("/api/deliveries/", {"pending": 1}).json() == []
    with db_context(tenant_id=a["tenant"].id):
        assert AuditLog.objects.filter(action="delivery_details_added").count() == 2


def test_the_form_can_be_saved_in_full_with_supplier_value_and_note_number():
    a, fc, mgr, alert, *_ = missing_delivery()
    form = alert["delivery_form"]
    res = mgr.post(f"/api/alerts/{alert['id']}/record-delivery/", {"tank": form["tank"], "litres": "5980", "delivered_at": form["delivered_at"],
                                                                     "supplier": "Kariba Fuel Supplies", "note_number": "KFS-20431", "value": "8452.30"}, format="json")
    assert res.status_code == 200, res.content
    assert "to add" not in res.json()["resolution_note"]
    with db_context(tenant_id=a["tenant"].id):
        d = Delivery.objects.get()
        assert (d.supplier, d.note_number, d.value, d.currency, d.litres_received) == ("Kariba Fuel Supplies", "KFS-20431", Decimal("8452.30"), "USD", Decimal(5980))


def test_a_time_outside_the_dips_or_another_fuels_tank_is_refused_and_nothing_is_recorded():
    from .helpers import second_tank

    a, fc, mgr, alert, first, second = missing_delivery()
    diesel = second_tank(fc)
    url, form = f"/api/alerts/{alert['id']}/record-delivery/", alert["delivery_form"]
    late = mgr.post(url, {"tank": form["tank"], "litres": "6000", "delivered_at": (second + timedelta(minutes=5)).isoformat()}, format="json")
    assert late.status_code == 400 and "between the dips" in str(late.json())
    wrong = mgr.post(url, {"tank": str(diesel.pk), "litres": "6000", "delivered_at": form["delivered_at"]}, format="json")
    assert wrong.status_code == 400 and "tank" in wrong.json()
    with db_context(tenant_id=a["tenant"].id):
        assert not Delivery.objects.exists() and Alert.objects.get(kind="delivery").status == "open"


def test_attendants_cannot_record_or_complete_deliveries():
    a, fc, mgr, alert, *_ = missing_delivery()
    att = client_for(a[Role.ATTENDANT])
    form = alert["delivery_form"]
    assert att.post(f"/api/alerts/{alert['id']}/record-delivery/", {"tank": form["tank"], "litres": "6000", "delivered_at": form["delivered_at"]},
                    format="json").status_code == 403
    mgr.post(f"/api/alerts/{alert['id']}/record-delivery/", {"tank": form["tank"], "litres": "6000", "delivered_at": form["delivered_at"]}, format="json")
    did = mgr.get("/api/deliveries/", {"pending": 1}).json()[0]["id"]
    assert att.patch(f"/api/deliveries/{did}/", {"supplier": "Me"}, format="json").status_code == 403
    assert mgr.patch("/api/deliveries/not-an-id/", {"supplier": "X"}, format="json").status_code == 404


def test_a_delivery_recorded_on_the_dip_screen_can_carry_its_value():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    res = client_for(a[Role.MANAGER]).post("/api/deliveries/", {"tank": str(fc.tank.pk), "litres": "5000", "supplier": "KFS", "note_number": "1",
                                                                "value": "7100"}, format="json")
    assert res.status_code == 201 and (res.json()["value"], res.json()["currency"], res.json()["details_pending"]) == ("7100.00", "USD", False)


def joined_missing_delivery():
    """Two petrol tanks a nozzle draws from together. Between two dips Tank 1 rose 1,000 L and Tank 2 rose 3,000 L
    while 400 L were sold: 4,400 L came in and nobody recorded it."""
    from .helpers import run_shift, second_tank

    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    tank2 = second_tank(fc, code="PETROL", name="Tank 2")
    owner = client_for(a[Role.OWNER])
    for n in fc.nozzles:
        assert owner.patch(f"/api/nozzles/{n.pk}/", {"also_draws_from": [str(tank2.pk)]}, format="json").status_code == 200
    now = timezone.now()
    first, second = now - timedelta(hours=10), now - timedelta(hours=2)
    with db_context(tenant_id=a["tenant"].id):
        from apps.operations import services

        for tank, before, after in ((fc.tank, 2000, 3000), (tank2, 1000, 4000)):
            services.record_tank_dip(user=a[Role.OWNER], tank=tank, litres=Decimal(before), at=first)
            services.record_tank_dip(user=a[Role.OWNER], tank=tank, litres=Decimal(after), at=second)
    run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [400, 0], started_at=now - timedelta(hours=8), ended_at=now - timedelta(hours=6),
              cash=[("cash", "USD", 600)])
    with db_context(tenant_id=a["tenant"].id):
        evaluate_tenant(a["tenant"].id)
    mgr = client_for(a[Role.MANAGER])
    alert = next(x for x in mgr.get("/api/alerts/").json()["results"] if x["kind"] == "delivery")
    return a, fc, tank2, mgr, alert


def test_joined_tanks_the_form_splits_the_litres_by_how_much_each_tanks_dip_rose():
    a, fc, tank2, mgr, alert = joined_missing_delivery()
    form = alert["delivery_form"]
    assert form["litres"] == "4400"
    assert form["split"] == [{"tank": str(fc.tank.pk), "name": "Tank 1", "rise": "1000", "litres": "1100"},
                             {"tank": str(tank2.pk), "name": "Tank 2", "rise": "3000", "litres": "3300"}]


def test_saving_the_split_records_one_delivery_per_tank_and_shares_the_value():
    a, fc, tank2, mgr, alert = joined_missing_delivery()
    form = alert["delivery_form"]
    res = mgr.post(f"/api/alerts/{alert['id']}/record-delivery/", {
        "split": [{"tank": p["tank"], "litres": p["litres"]} for p in form["split"]], "delivered_at": form["delivered_at"],
        "supplier": "Sogo", "note_number": "DN-9", "value": "6600"}, format="json")
    assert res.status_code == 200, res.content
    body = res.json()
    assert body["status"] == "resolved" and body["resolution_note"] == "Recorded: 1,100 L into Tank 1 and 3,300 L into Tank 2."
    with db_context(tenant_id=a["tenant"].id):
        rows = {d.tank_id: d for d in Delivery.objects.all()}
        assert (rows[fc.tank.pk].litres_received, rows[fc.tank.pk].value) == (Decimal(1100), Decimal("1650.00"))
        assert (rows[tank2.pk].litres_received, rows[tank2.pk].value) == (Decimal(3300), Decimal("4950.00"))
        assert {d.supplier for d in rows.values()} == {"Sogo"} and AuditLog.objects.filter(action="delivery_recorded").count() == 2
        evaluate_tenant(a["tenant"].id)  # the fuel check balances: nothing comes back
        assert not Alert.objects.filter(kind="delivery", status="open").exists()


def test_a_split_into_a_tank_of_another_fuel_or_with_no_litres_is_refused():
    from .helpers import second_tank

    a, fc, tank2, mgr, alert = joined_missing_delivery()
    diesel = second_tank(fc, code="DIESEL", name="Diesel Tank")
    form, url = alert["delivery_form"], f"/api/alerts/{alert['id']}/record-delivery/"
    wrong = mgr.post(url, {"split": [{"tank": str(diesel.pk), "litres": "4400"}], "delivered_at": form["delivered_at"]}, format="json")
    assert wrong.status_code == 400
    empty = mgr.post(url, {"split": [{"tank": str(fc.tank.pk), "litres": "0"}, {"tank": str(tank2.pk), "litres": "0"}], "delivered_at": form["delivered_at"]}, format="json")
    assert empty.status_code == 400
    with db_context(tenant_id=a["tenant"].id):
        assert not Delivery.objects.exists()
