"""The monthly accounting pack: one Excel workbook per station and month, for the bookkeeper.

Sheets: Sales (per day and fuel), Takings (per day and payment type, converted at the owner's rates), Cash differences
(per shift and attendant), Deliveries (purchases), Stock (per tank and day) and Prices (in force during the month).
Built from the same records and rules as the Reports screens, so the two always agree. Deliveries carry no cost yet:
those two columns stay empty until deliveries record what was paid.
"""
import io
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from apps.operations import tradingday
from apps.operations.models import CashSubmission, Delivery, Shift, Transaction
from apps.stations.models import FuelPrice, Tank
from apps.stations.services import rate_at
from apps.tenants.services import get_settings

from .reconciliation import cash_summary, reconcile_tank_day, tank_totals

HEAD = Font(bold=True, color="FFFFFF")
HEAD_FILL = PatternFill("solid", fgColor="166534")
GAIN_FILL = PatternFill("solid", fgColor="DCFCE7")
LOSS_FILL = PatternFill("solid", fgColor="FEE4E2")
Q2, Q3 = Decimal("0.01"), Decimal("0.001")
PAYMENT_LABEL = {"cash": "Cash", "mobile_money": "Mobile money", "card": "Card", "credit": "Intercompany issue (owed by the other company)"}
INTERCOMPANY = "credit"  # fuel issued to another company on account: not money received, money owed to the business


def month_bounds(year, month, cal=None):
    """The month's trading days (each 21:00 to 21:00, named by its closing day) and the moments they span."""
    cal = cal or tradingday.Calendar()
    first, last = date(year, month, 1), date(year, month, monthrange(year, month)[1])
    return first, last, cal.bounds(first)[0], cal.bounds(last)[1]


def _local(dt):
    return timezone.localtime(dt).replace(tzinfo=None) if dt else None


def _sheet(wb, title, station, period, columns, rows, fills=None):
    ws = wb.create_sheet(title)
    ws.append([f"{station.name}: {title}", period])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])
    ws.append(columns)
    for cell in ws[3]:
        cell.font, cell.fill = HEAD, HEAD_FILL
    for i, row in enumerate(rows):
        ws.append(row)
        if fills and fills[i]:
            for cell in ws[ws.max_row]:
                cell.fill = fills[i]
    if not rows:
        ws.append(["Nothing recorded in this month."])
    for i, name in enumerate(columns, start=1):
        width = max([len(str(name))] + [len(str(r[i - 1])) for r in rows if i - 1 < len(r) and r[i - 1] is not None]) + 2
        ws.column_dimensions[get_column_letter(i)].width = min(max(width, 10), 48)
    ws.freeze_panes = "A4"
    return ws


def build_pack(station, year, month):
    """The workbook for one station and month, as .xlsx bytes. Call inside the tenant's database context."""
    cal = tradingday.Calendar(station.pk)
    first, last, start, end = month_bounds(year, month, cal)
    period = f"{first:%B %Y}"
    currency = station.pricing_currency
    cfg = get_settings(station.tenant_id)
    wb = Workbook()
    wb.remove(wb.active)

    # Sales: per day (local time) and fuel, from the meter readings (the same as the Sales report)
    by_day = {}
    for t in Transaction.objects.filter(station=station, occurred_at__gt=start, occurred_at__lte=end).select_related("fuel_type"):
        key = (cal.day_of(t.occurred_at), t.fuel_type.name, t.currency)
        litres, amount = by_day.get(key, (Decimal(0), Decimal(0)))
        by_day[key] = (litres + t.litres, amount + t.amount)
    rows = []
    for (day, fuel, cur), (litres, amount) in sorted(by_day.items()):
        rows.append([day, fuel, float(litres.quantize(Q3)), float((amount / litres).quantize(Decimal("0.0001"))) if litres else None, float(amount.quantize(Q2)), cur])
    if rows:
        rows.append(["Total", "", float(sum((r[2] for r in rows), 0.0)), None, float(sum((r[4] for r in rows), 0.0)), currency])
    _sheet(wb, "Sales", station, period, ["Date", "Fuel", "Litres", "Price per litre", "Amount", "Currency"], rows)

    # Takings: what was handed in, per day and payment type, converted at the owner's rate on that day
    takings = {}
    handed_in = (Q(part__cash_submitted_at__gt=start, part__cash_submitted_at__lte=end)
                 | Q(part__cash_submitted_at__isnull=True, created_at__gt=start, created_at__lte=end))  # older rows without a part
    for c in CashSubmission.objects.filter(shift__station=station).filter(handed_in).select_related("part"):
        when = c.part.cash_submitted_at if c.part_id and c.part.cash_submitted_at else c.created_at
        key = (cal.day_of(when), c.payment_type, c.currency)
        takings[key] = takings.get(key, Decimal(0)) + c.amount
    rows, received, owed = [], Decimal(0), Decimal(0)
    for (day, kind, cur), amount in sorted(takings.items()):
        rate = rate_at(cur, currency, cal.bounds(day)[1] - timedelta(seconds=1))
        converted = (amount * rate).quantize(Q2) if rate is not None else None
        if kind == INTERCOMPANY:
            owed += converted or 0
        else:
            received += converted or 0
        rows.append([day, PAYMENT_LABEL.get(kind, kind), cur, float(amount.quantize(Q2)), float(rate) if rate is not None else "no rate set",
                     float(converted) if converted is not None else None])
    if rows:
        rows.append(["Money received", "", "", None, "", float(received)])
        rows.append([f"Owed to {station.tenant.name} (intercompany issues)", "", "", None, "", float(owed)])
        rows.append(["Total handed in", "", "", None, "", float(received + owed)])
    _sheet(wb, "Takings", station, period, ["Date", "Payment type", "Currency", "Amount", "Rate", f"Amount in {currency}"], rows)

    # Cash differences: per closed shift (ended in the month) and attendant
    rows, fills = [], []
    for s in Shift.objects.filter(station=station, ended_at__gt=start, ended_at__lte=end).exclude(status="open").order_by("started_at"):
        c = cash_summary(s)
        people = c["by_attendant"] if c["per_attendant_expected"] else [{"name": "Everyone on the shift", "expected": c["expected"],
                                                                          "submitted": c["submitted_total"], "cash_difference": c["cash_difference"], "status": c["status"]}]
        for p in people:
            diff = Decimal(p.get("cash_difference") or 0)
            rows.append([_local(s.started_at), _local(s.ended_at), p["name"], float(Decimal(p.get("expected") or 0)), float(Decimal(p["submitted"])), float(diff),
                         {"ok": "OK", "short": "Short", "over": "Over", "incomplete": "Missing exchange rate"}.get(p.get("status"), p.get("status") or "")])
            fills.append(LOSS_FILL if p.get("status") == "short" else GAIN_FILL if p.get("status") == "over" else None)
    _sheet(wb, "Cash differences", station, period, ["Shift started", "Shift ended", "Attendant", f"Expected ({currency})", f"Handed in ({currency})", "Difference", "Result"],
           rows, fills)

    # Deliveries (purchases)
    rows = [[_local(d.delivered_at), d.tank.name, d.tank.fuel_type.name, d.supplier, d.note_number, float(d.litres_received), None, None]
            for d in Delivery.objects.filter(tank__station=station, delivered_at__gt=start, delivered_at__lte=end).select_related("tank__fuel_type").order_by("delivered_at")]
    if rows:
        rows.append(["Total", "", "", "", "", float(sum(r[5] for r in rows)), None, None])
    _sheet(wb, "Deliveries", station, period, ["Date", "Tank", "Fuel", "Supplier", "Delivery note", "Litres", "Cost per litre", "Total cost"], rows)

    # Stock: per tank and day, between the actual dips (the same as the Fuel check report)
    rows, fills, checks = [], [], []
    for tank in Tank.objects.filter(station=station).select_related("fuel_type").order_by("name"):
        day = first
        while day <= min(last, cal.today()):
            r = reconcile_tank_day(tank, day, cfg)
            if r["status"] != "insufficient_data":
                checks.append(r)
                rows.append([day, tank.name, tank.fuel_type.name, float(Decimal(r["opening"])), float(Decimal(r["deliveries"])), float(Decimal(r["dispensed"])),
                             float(Decimal(r["closing"])), float(Decimal(r["loss"])), {"ok": "OK", "loss": "Loss", "gain": "Gain"}[r["status"]]])
                fills.append(LOSS_FILL if r["status"] == "loss" else GAIN_FILL if r["status"] == "gain" else None)
            day += timedelta(days=1)
    # the running difference per tank for the month: a loss and the matching gain from a badly timed dip cancel here
    for t in tank_totals(checks):
        delivered = sum((Decimal(r["deliveries"]) for r in checks if r["tank_id"] == t["tank_id"]), Decimal(0))
        rows.append(["Month total", t["tank"], t["fuel_code"].title(), None, float(delivered), float(Decimal(t["sold"])), None, float(Decimal(t["unexplained"])),
                     {"ok": "OK", "loss": "Loss", "gain": "Gain"}[t["status"]] + (f" ({t['pct']}% of sold)" if t["pct"] else "")])
        fills.append(LOSS_FILL if t["status"] == "loss" else GAIN_FILL if t["status"] == "gain" else None)
    _sheet(wb, "Stock", station, period, ["Date", "Tank", "Fuel", "Opening dip (L)", "Deliveries (L)", "Sold (L)", "Closing dip (L)",
                                          "Unexplained (L, + loss, - gain)", "Result"], rows, fills)

    # Prices in force during the month
    prices = (FuelPrice.objects.filter(station=station, effective_from__lt=end).filter(Q(effective_to__isnull=True) | Q(effective_to__gt=start))
              .select_related("fuel_type").order_by("fuel_type__name", "effective_from"))
    rows = [[p.fuel_type.name, p.currency, float(p.price_per_litre), _local(p.effective_from), _local(p.effective_to) or "still in force"] for p in prices]
    _sheet(wb, "Prices", station, period, ["Fuel", "Currency", "Price per litre", "From", "To"], rows)

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
