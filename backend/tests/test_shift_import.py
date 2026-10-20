"""Owner imports past shifts from CSV: checked first, all or nothing, priced from history, never merged into a live shift."""
from datetime import timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.accounts.models import Role
from apps.audit.models import AuditLog
from apps.core.db import db_context
from apps.operations import services
from apps.operations.models import CashSubmission, DailySalesRollup, PumpMeterReading, Shift, ShiftSummary, Transaction
from apps.stations.models import FuelPrice
from apps.stations.services import price_at

from .helpers import client_for, make_forecourt, two_tenants

URL = "/api/shifts/import/"
HEAD = "station,date,start_time,end_time,attendants,pump,nozzle,opening_meter,closing_meter,price_per_litre,pump_attendant\n"
CASH_HEAD = "station,date,start_time,attendant,payment_type,currency,amount\n"


def _day(days_ago):
    return (timezone.localdate() - timedelta(days=days_ago)).isoformat()


def _send(user, meters, cash=None, commit=True):
    files = {"meters": SimpleUploadedFile("meters.csv", meters.encode(), "text/csv")}
    if cash is not None:
        files["cash"] = SimpleUploadedFile("cash.csv", cash.encode(), "text/csv")
    return client_for(user).post(URL, {**files, "commit": "true" if commit else "false"}, format="multipart")


def _shift_rows(day, who, start="06:00", end="18:00", n1=(1000, 1100), n2=(500, 550), price=""):
    return (f"Main Station,{day},{start},{end},{who},Pump 1,1,{n1[0]},{n1[1]},{price},\n"
            f"Main Station,{day},{start},{end},{who},Pump 1,2,{n2[0]},{n2[1]},,\n")


def test_only_the_owner_can_import_or_get_the_templates():
    a, _ = two_tenants()
    make_forecourt(a["tenant"])
    for role in (Role.MANAGER, Role.ATTENDANT):
        assert _send(a[role], HEAD).status_code == 403
        assert client_for(a[role]).get(f"{URL}template/").status_code == 403
    res = client_for(a[Role.OWNER]).get(f"{URL}template/")
    assert res.status_code == 200
    lines = res.content.decode().splitlines()
    assert lines[0] == HEAD.strip() and lines[1].startswith("Main Station,,,,,Pump 1,1,") and len(lines) == 3
    assert client_for(a[Role.OWNER]).get(f"{URL}template/?file=cash").content.decode().strip() == CASH_HEAD.strip()


def test_preview_saves_nothing_then_import_builds_sales_cash_and_rollups():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    who = a[Role.ATTENDANT].full_name
    meters = HEAD + _shift_rows(_day(10), who)
    cash = CASH_HEAD + f"Main Station,{_day(10)},06:00,{who},cash,USD,200\nMain Station,{_day(10)},06:00,{who},EcoCash,,25\n"

    preview = _send(a[Role.OWNER], meters, cash, commit=False)
    assert preview.status_code == 200, preview.content
    body = preview.json()
    assert body["saved"] is False and body["shifts"] == 1 and body["litres"] == "150.000" and body["amount_by_currency"] == {"USD": "225.00"}
    with db_context(tenant_id=a["tenant"].id):
        assert not Shift.objects.exists() and not AuditLog.objects.filter(action="shifts_imported").exists()

    res = _send(a[Role.OWNER], meters, cash)
    assert res.status_code == 201, res.content
    assert res.json()["saved"] is True and res.json()["flagged_shifts"] == 0
    with db_context(tenant_id=a["tenant"].id):
        shift = Shift.objects.get()
        assert shift.status == Shift.Status.CLOSED and timezone.localtime(shift.started_at).hour == 6 and "Imported from meters.csv" in shift.notes
        assert [p.user_id for p in shift.attendants.all()] == [a[Role.ATTENDANT].id]
        assert set(PumpMeterReading.objects.values_list("source", flat=True)) == {"import"}
        assert sum(t.amount for t in Transaction.objects.filter(shift=shift)) == Decimal("225.00")
        assert {(c.payment_type, c.amount) for c in CashSubmission.objects.all()} == {("cash", Decimal("200.00")), ("mobile_money", Decimal("25.00"))}
        assert ShiftSummary.objects.get(shift=shift).flagged is False
        assert DailySalesRollup.objects.get(station=fc.station).amount == Decimal("225.00")
        assert AuditLog.objects.get(action="shifts_imported").changes["shifts"] == 1

    again = _send(a[Role.OWNER], meters, cash)  # the same file twice adds nothing
    assert again.status_code == 400 and "overlaps" in again.json()["errors"][0]["message"]


def test_old_prices_fill_gaps_in_history_but_never_contradict_it_or_change_todays_price():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")  # history starts 60 days ago
    who = a[Role.ATTENDANT].full_name

    assert "No Petrol price" in _send(a[Role.OWNER], HEAD + _shift_rows(_day(100), who)).json()["errors"][0]["message"]
    wrong = _send(a[Role.OWNER], HEAD + _shift_rows(_day(10), who, price="1.60")).json()["errors"][0]["message"]
    assert "was 1.5 USD" in wrong and "file says 1.6" in wrong

    meters = HEAD + _shift_rows(_day(100), who, price="1.20") + _shift_rows(_day(90), who, n1=(1100, 1200), n2=(550, 600), price="1.30")
    res = _send(a[Role.OWNER], meters)
    assert res.status_code == 201, res.content
    assert [p["price_per_litre"] for p in res.json()["prices_added"]] == ["1.20", "1.30"]
    with db_context(tenant_id=a["tenant"].id):
        old = list(FuelPrice.objects.filter(station=fc.station).order_by("effective_from"))
        assert [p.price_per_litre for p in old] == [Decimal("1.2000"), Decimal("1.3000"), Decimal("1.5000")]
        assert old[0].effective_to == old[1].effective_from and old[1].effective_to == old[2].effective_from  # no gaps, no overlaps
        assert price_at(fc.station.pk, fc.fuel.pk, "USD", timezone.now()).price_per_litre == Decimal("1.5000")
        assert sorted(sum(t.amount for t in s.transactions.all()) for s in Shift.objects.all()) == [Decimal("180.00"), Decimal("195.00")]


def test_every_problem_comes_back_with_its_row_and_nothing_is_saved():
    a, _ = two_tenants()
    make_forecourt(a["tenant"])
    who = a[Role.ATTENDANT].full_name
    meters = (HEAD
              + f"Nowhere,{_day(5)},06:00,18:00,{who},Pump 1,1,1,2,,\n"
              + f"Main Station,{_day(5)},06:00,18:00,Nobody,Pump 1,1,1,2,,\n"
              + f"Main Station,{_day(4)},06:00,18:00,{who},Pump 1,1,900,800,,\n"
              + f"Main Station,{timezone.localdate().isoformat()},00:00,23:59,{who},Pump 1,1,1,2,,\n"
              + f"Main Station,{_day(3)},06:00,18:00,{who},Pump 1,1,1,2,,\n")
    cash = CASH_HEAD + f"Main Station,{_day(20)},06:00,{who},cash,USD,10\n"
    res = _send(a[Role.OWNER], meters, cash)
    assert res.status_code == 400
    errors = {(e["file"], e["row"]): e["message"] for e in res.json()["errors"]}
    assert "No station called 'Nowhere'" in errors[("meters", 2)]
    assert "No attendant called 'Nobody'" in errors[("meters", 3)]
    assert "below opening meter" in errors[("meters", 4)]
    assert "has not ended yet" in errors[("meters", 5)]
    assert "No shift at Main Station" in errors[("cash", 2)]
    with db_context(tenant_id=a["tenant"].id):
        assert not Shift.objects.exists()


def test_night_shifts_day_first_dates_and_semicolon_files_from_excel():
    a, _ = two_tenants()
    make_forecourt(a["tenant"])
    who = a[Role.ATTENDANT].full_name
    day = timezone.localdate() - timedelta(days=7)
    meters = HEAD.replace(",", ";") + f"Main Station;{day:%d/%m/%Y};18:00;06:00;{who};Pump 1;Petrol;1 000,5;1 100,5;;\n"
    res = _send(a[Role.OWNER], meters)
    assert res.status_code == 201, res.content
    with db_context(tenant_id=a["tenant"].id):
        shift = Shift.objects.get()
        assert shift.ended_at - shift.started_at == timedelta(hours=12)
        assert Transaction.objects.get().litres == Decimal("100.000")


def test_an_old_shift_is_never_merged_into_the_shift_open_today():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    with db_context(tenant_id=a["tenant"].id):
        live = services.start_shift(user=a[Role.ATTENDANT], station=fc.station, readings=[{"nozzle": fc.nozzles[0], "reading": Decimal("5000")}])
    assert _send(a[Role.OWNER], HEAD + _shift_rows(_day(10), a[Role.ATTENDANT].full_name)).status_code == 201
    with db_context(tenant_id=a["tenant"].id):
        assert Shift.objects.count() == 2
        assert Shift.objects.get(pk=live.pk).status == Shift.Status.OPEN
        assert not PumpMeterReading.objects.filter(shift=live, source="import").exists()


def test_another_business_names_are_unknown():
    a, b = two_tenants()
    make_forecourt(a["tenant"])
    res = _send(b[Role.OWNER], HEAD + _shift_rows(_day(10), a[Role.ATTENDANT].full_name))
    assert res.status_code == 400
    assert "No station called 'Main Station'" in res.json()["errors"][0]["message"]
    with db_context(tenant_id=a["tenant"].id):
        assert not Shift.objects.exists()
