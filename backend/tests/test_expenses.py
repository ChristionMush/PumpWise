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
