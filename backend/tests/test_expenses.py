"""Expenses: money paid out at a station (lunch, generator fuel...). Switched on per business by the platform; recorded
by attendants and managers; recorded only, so the cash an attendant hands in is unchanged."""
import io
import uuid

from openpyxl import load_workbook

from apps.accounts.models import Role
from apps.core.db import db_context
from apps.operations.models import Expense

from .helpers import at, client_for, make_forecourt, platform_client, run_shift, two_tenants
from .test_multi_attendant import op


def expenses(a, on=True):
    res = platform_client().patch(f"/api/platform/tenants/{a['tenant'].id}/features/", {"expenses_enabled": on}, format="json")
    assert res.status_code == 200 and res.json()["features"]["expenses_enabled"] is on


def test_expenses_are_off_until_the_platform_turns_them_on():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    body = {"station": str(fc.station.pk), "category": "lunch", "amount": "5"}
    refused = client_for(a[Role.ATTENDANT]).post("/api/expenses/", body, format="json")
    assert refused.status_code == 400 and "not switched on" in str(refused.json())
    assert client_for(a[Role.ATTENDANT]).get("/api/auth/me/").json()["tenant"]["expenses_enabled"] is False
    expenses(a)
    assert client_for(a[Role.ATTENDANT]).post("/api/expenses/", body, format="json").status_code == 201
    assert client_for(a[Role.OWNER]).patch(f"/api/platform/tenants/{a['tenant'].id}/features/", {"expenses_enabled": False}, format="json").status_code == 403


def test_an_attendant_records_an_expense_on_their_shift_and_their_cash_is_unchanged():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    expenses(a)
    att = a[Role.ATTENDANT]
    shift = run_shift(a["tenant"], att, fc, [0, 0], [100, 0], started_at=at(2026, 10, 1, 5), ended_at=at(2026, 10, 1, 15), cash=[("cash", "USD", 150)])
    c = client_for(att)
    cid = str(uuid.uuid4())
    body = {"client_id": cid, "station": str(fc.station.pk), "category": "generator_fuel", "amount": "12.50", "note": "20 L diesel", "spent_at": "2026-10-01T09:00:00Z"}
    made = c.post("/api/expenses/", body, format="json")
    assert made.status_code == 201, made.content
    assert made.json()["shift"] == str(shift.pk) and made.json()["recorded_by_name"] == att.full_name and made.json()["currency"] == "USD"
    assert c.post("/api/expenses/", body, format="json").status_code == 200  # a retry never records it twice
    # offline: the same through the sync queue
    synced = c.post("/api/sync/", {"ops": [op("expense", {"client_id": str(uuid.uuid4()), "station": str(fc.station.pk), "category": "lunch", "amount": "4"})]}, format="json")
    assert synced.json()["results"][0]["status"] == "applied", synced.content
    # "other" needs a reason; nothing negative
    assert c.post("/api/expenses/", {**body, "client_id": None, "category": "other", "note": ""}, format="json").status_code == 400
    assert c.post("/api/expenses/", {**body, "client_id": None, "amount": "0"}, format="json").status_code == 400
    from apps.operations.models import CashSubmission

    with db_context(tenant_id=a["tenant"].id):
        assert Expense.objects.count() == 2
        # recorded only: what the attendant handed in is exactly as before
        assert list(CashSubmission.objects.filter(shift=shift).values_list("payment_type", "amount")) == [("cash", 150)]

    # the attendant sees their own; the manager and owner see all, the accounting pack lists them
    other = a[Role.MANAGER]
    client_for(other).post("/api/expenses/", {"station": str(fc.station.pk), "category": "repairs", "amount": "30", "spent_at": "2026-10-01T10:00:00Z"}, format="json")
    assert len(c.get("/api/expenses/").json()) == 2
    listed = client_for(a[Role.OWNER]).get("/api/expenses/", {"station": str(fc.station.pk)}).json()
    assert {e["category_label"] for e in listed} == {"Generator fuel", "Lunch", "Repairs and maintenance"}
    res = client_for(a[Role.OWNER]).get("/api/reports/accounting-pack/", {"station": str(fc.station.pk), "month": "2026-10"})
    rows = list(load_workbook(io.BytesIO(res.content))["Expenses"].iter_rows(min_row=4, values_only=True))
    assert rows[0][2:] == (att.full_name, "Generator fuel", "20 L diesel", "USD", 12.5, 12.5)
    assert rows[-1][0] == "Total expenses" and rows[-1][7] == 42.5  # 12.50 + 30 in October (the synced lunch is today)


def test_a_business_without_expenses_gets_no_expenses_sheet():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    res = client_for(a[Role.OWNER]).get("/api/reports/accounting-pack/", {"station": str(fc.station.pk), "month": "2026-10"})
    assert "Expenses" not in load_workbook(io.BytesIO(res.content)).sheetnames


def test_an_attendants_expenses_come_off_the_cash_they_hand_in_everywhere_it_is_checked():
    from datetime import date

    from apps.operations.models import Shift
    from apps.reports.daily import daily_report
    from apps.reports.reconciliation import cash_summary

    from .test_multi_attendant import cash, readings

    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    expenses(a)
    ca = client_for(a[Role.ATTENDANT])
    sid = ca.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": "2026-10-01T05:00:00Z", "readings": readings(fc.nozzles, [0, 0])},
                  format="json").json()["id"]
    # meters say 150; the attendant paid 5 for lunch out of it, so hands in 145
    ended = ca.post(f"/api/shifts/{sid}/end/", {"ended_at": "2026-10-01T15:00:00Z", "readings": readings(fc.nozzles, [100, 0]), "cash": cash("145"),
                                                 "expenses": [{"client_id": str(uuid.uuid4()), "name": "Lunch", "amount": "5"}]}, format="json")
    assert ended.status_code == 200, ended.content
    with db_context(tenant_id=a["tenant"].id):
        c = cash_summary(Shift.objects.get(pk=sid))
        assert (c["expected"], c["submitted_total"], c["expenses"], c["cash_difference"], c["status"]) == ("150.00", "145.00", "5.00", "0.00", "ok")
        report = daily_report(fc.station, date(2026, 10, 1))
    lines = {h["label"]: h["amount"] for h in report["handed_in"]}
    assert lines["Cash"] == "145.00" and lines["Expenses paid out"] == "5.00"
    assert not [b for b in report["balance"] if b["key"] == "short"]  # not short: the 5 is accounted for
    assert [(s["handed_in"], s["cash_difference"]) for s in report["shifts"]] == [("145.00", "0.00")]  # the shift's line agrees with its people
    card = next(e for e in client_for(a[Role.MANAGER]).get("/api/shift-ends/").json() if e["shift_id"] == sid)
    assert (card["expected"], card["handed_in"], card["expenses_total"], card["cash_difference"]) == ("150.00", "145.00", "5.00", "0.00")
    # the report by attendant agrees: it once left the expenses out and showed the 5 as a cash difference
    row = client_for(a[Role.MANAGER]).get("/api/reports/attendants/", {"date_from": "2026-10-01", "date_to": "2026-10-01"}).json()["rows"][0]
    assert (row["sales"], row["handed_in"], row["expenses"], row["expected"], row["cash_difference"]) == ("150.00", "145.00", "5.00", "150.00", "0.00")


def test_a_finished_part_of_a_running_shift_counts_its_expenses_in_the_report_by_attendant():
    from .test_multi_attendant import T0, T_SWAP, assigned_site, cash, readings

    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    expenses(a)
    ca, cb = client_for(att_a), client_for(att_b)
    body = {"station": str(fc.station.pk), "started_at": T0}
    sid = ca.post("/api/shifts/start/", {**body, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    cb.post("/api/shifts/start/", {**body, "readings": readings(p2, [0, 0])}, format="json")
    # A's part: meters say 75, A paid 5 for lunch and hands in 70. B is still on, so the shift stays open.
    ended = ca.post(f"/api/shifts/{sid}/end/", {"ended_at": T_SWAP, "readings": readings(fc.nozzles, [50, 0]), "cash": cash("70"),
                                                 "expenses": [{"client_id": str(uuid.uuid4()), "name": "Lunch", "amount": "5"}]}, format="json")
    assert ended.status_code == 200 and ended.json()["status"] == "open", ended.content
    row = {r["name"]: r for r in mgr.get("/api/reports/attendants/", {"date_from": "2026-10-01", "date_to": "2026-10-01"}).json()["rows"]}[att_a.full_name]
    assert (row["handed_in"], row["expenses"], row["expected"], row["cash_difference"], row["flagged_shifts"]) == ("70.00", "5.00", "75.00", "0.00", 0)


def test_expenses_sent_after_the_cash_clear_the_dashboards_cash_difference():
    """PESNAT, 14 and 15 Nov 2026: the phone sends the cash, then its expenses a moment later. The shift's stored cash
    check (the dashboard's "Cash differences (7 days)") was worked out with the cash alone and stayed short."""
    from decimal import Decimal

    from apps.operations import services
    from apps.operations.models import Shift, ShiftSummary

    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    expenses(a)
    # meters say 150; the attendant hands in 100 and paid 50 for the generator
    run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [100, 0], started_at=at(2026, 10, 1, 5), ended_at=at(2026, 10, 1, 15), cash=[("cash", "USD", 100)])
    with db_context(tenant_id=a["tenant"].id):
        shift = Shift.objects.get()
        assert ShiftSummary.objects.get(shift=shift).flagged  # 50 short, with the cash alone
        services.record_expense(user=a[Role.ATTENDANT], station=fc.station, category="other", amount=Decimal(50), note="Generator",
                                spent_at=at(2026, 10, 1, 15), shift=shift)
        summary = ShiftSummary.objects.get(shift=shift)
        assert not summary.flagged and summary.cash["expenses"] == "50.00" and summary.cash["cash_difference"] == "0.00"
