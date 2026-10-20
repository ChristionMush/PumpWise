"""Import past shifts from CSV files (owner only).

Old records (paper books, spreadsheets) come in as two CSV files saved from Excel:

* meters (required): one row per nozzle per shift. Columns: station, date, start_time, end_time, attendants
  (names or phone numbers, separated by ";"), pump, nozzle (number or label), opening_meter, closing_meter, and
  optionally price_per_litre and pump_attendant (who held the pump, on assigned-pump stations).
* cash (optional): one row per attendant per payment type. Columns: station, date, start_time, attendant,
  payment_type (cash, ecocash / mobile money, card / swipe, credit / coupon), currency (default: the station's), amount.

Every row is checked first and nothing is saved unless the whole upload is clean, so a file can be fixed and sent
again. A past shift is written straight in as a closed shift, never through the live start/end flow (which would merge
it into a shift open today). The same code a live shift runs when it closes then builds its sales, cash check and
dashboard numbers. Prices come from price history (rule 4): where history has no price for a shift, the file's
price fills that gap; where history has a different price, the row is refused. The current price is never changed.
A shift that overlaps one already in the system is refused, so sending the same file twice adds nothing.
"""
import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction as db_tx
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.accounts.models import Role, User
from apps.accounts.utils import normalize_phone
from apps.audit import services as audit
from apps.stations.models import FuelPrice, Nozzle, Pump, Station

from .models import CashSubmission, PumpAssignment, PumpMeterReading, Shift, ShiftAttendant, Transaction
from .rollups import deferred, on_shift_closed
from .services import compute_transactions, dispensed_litres

METER_COLUMNS = ["station", "date", "start_time", "end_time", "attendants", "pump", "nozzle", "opening_meter", "closing_meter", "price_per_litre", "pump_attendant"]
METER_REQUIRED = METER_COLUMNS[:9]
CASH_COLUMNS = ["station", "date", "start_time", "attendant", "payment_type", "currency", "amount"]
CASH_REQUIRED = ["station", "date", "start_time", "attendant", "payment_type", "amount"]
MAX_BYTES = 5 * 1024 * 1024
MAX_METER_ROWS = 5000  # about a year of one busy station; larger histories go in a few uploads
MAX_ERRORS = 50
PAYMENT_TYPES = {
    "cash": "cash", "mobile_money": "mobile_money", "mobile money": "mobile_money", "ecocash": "mobile_money",
    "card": "card", "swipe": "card", "card / swipe": "card", "credit": "credit", "coupon": "credit", "credit / coupon": "credit",
}
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y")
TIME_FORMATS = ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p")


class _DryRun(Exception):
    """Raised to roll back a preview after everything has been built and counted."""


@dataclass
class _Shift:
    station: Station
    start: datetime
    end: datetime
    people: list
    first_row: int
    readings: list = field(default_factory=list)  # (row, nozzle, opening, closing, file_price, pump_attendant)
    cash: list = field(default_factory=list)  # (row, user, payment_type, currency, amount)
    holders: dict = field(default_factory=dict)  # assigned pumps: pump id -> who held it


class _Errors:
    def __init__(self):
        self.items = []

    def add(self, file, row, message):
        if len(self.items) < MAX_ERRORS:
            self.items.append({"file": file, "row": row, "message": message})

    def raise_if_any(self):
        if self.items:
            raise ValidationError({"errors": self.items})


# ---- reading the files -------------------------------------------------------------------------------------------

def _key(header):
    return (header or "").strip().lower().replace(" ", "_")


def read_csv(upload, name, required):
    """[(row number, {column: value})], skipping blank rows. Accepts Excel's CSV (BOM, ';' in some locales)."""
    raw = upload.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValidationError({"errors": [{"file": name, "row": None, "message": "This file is too big. Import a few months at a time."}]})
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):  # latin-1 decodes anything
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    lines = text.splitlines()
    if not lines:
        raise ValidationError({"errors": [{"file": name, "row": None, "message": "This file is empty."}]})
    try:
        dialect = csv.Sniffer().sniff(lines[0], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    header = [_key(h) for h in next(reader)]
    if missing := [c for c in required if c not in header]:
        raise ValidationError({"errors": [{"file": name, "row": 1, "message": f"Missing column(s): {', '.join(missing)}. Start from the template."}]})
    rows = []
    for number, values in enumerate(reader, start=2):
        row = {h: (values[i].strip() if i < len(values) else "") for i, h in enumerate(header)}
        if any(row.values()):
            rows.append((number, row))
    return rows, dialect.delimiter == ";"


def _date(value):
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"'{value}' is not a date. Write it as 2026-03-31 or 31/03/2026 (day first).")


def _time(value):
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(value.upper(), fmt).time()
        except ValueError:
            continue
    raise ValueError(f"'{value}' is not a time. Write it as 06:00 or 18:30.")


def _number(value, what, decimal_comma):
    text = value.replace(" ", "")
    text = text.replace(".", "").replace(",", ".") if decimal_comma else text.replace(",", "")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{what} '{value}' is not a number.")
    if not number.is_finite() or number < 0:
        raise ValueError(f"{what} '{value}' must be zero or more.")
    return number


def _when(day, at):
    return timezone.make_aware(datetime.combine(day, at))


def _fmt(number):
    return format(number.normalize(), "f")


def _shown(dt):
    return timezone.localtime(dt).strftime("%d %b %Y %H:%M")


# ---- finding what the names in the file refer to -----------------------------------------------------------------

class _Lookup:
    """The business's stations, pumps, nozzles and attendants by the names people write (RLS keeps it to one tenant)."""

    def __init__(self):
        self.stations = {s.name.casefold(): s for s in Station.objects.all()}
        self.pumps = {(p.station_id, p.name.casefold()): p for p in Pump.objects.all()}
        self.nozzles = {}
        for n in Nozzle.objects.select_related("tank__fuel_type", "pump"):
            self.nozzles[(n.pump_id, str(n.number))] = n
            for label in {n.label, n.tank.fuel_type.name, n.tank.fuel_type.code}:
                if label:
                    self.nozzles.setdefault((n.pump_id, label.casefold()), n)
        self.by_name, self.by_phone = {}, {}
        for u in User.objects.filter(role=Role.ATTENDANT):  # former staff (switched off) still count for old shifts
            self.by_name.setdefault(u.full_name.strip().casefold(), []).append(u)
            if u.phone:
                self.by_phone[u.phone] = u

    def station(self, name):
        st = self.stations.get(name.casefold())
        if st is None:
            raise ValueError(f"No station called '{name}'.")
        return st

    def nozzle(self, station, pump_name, nozzle):
        pump = self.pumps.get((station.pk, pump_name.casefold()))
        if pump is None:
            raise ValueError(f"No pump called '{pump_name}' at {station.name}.")
        found = self.nozzles.get((pump.pk, nozzle.casefold()))
        if found is None:
            raise ValueError(f"{pump.name} has no nozzle '{nozzle}'. Use its number (1, 2) or its fuel.")
        return found

    def attendant(self, text):
        phone = normalize_phone(text)
        if phone and phone in self.by_phone:
            return self.by_phone[phone]
        people = self.by_name.get(text.strip().casefold(), [])
        if len(people) == 1:
            return people[0]
        if len(people) > 1:
            raise ValueError(f"Two attendants are called '{text}'. Use their phone number instead.")
        raise ValueError(f"No attendant called '{text}'. Add them under Settings first (they can be switched off afterwards).")


# ---- checking ------------------------------------------------------------------------------------------------------

def _parse_meters(rows, decimal_comma, lookup, errors):
    shifts = {}
    now = timezone.now()
    for number, r in rows:
        if not (r.get("date") or r.get("opening_meter") or r.get("closing_meter")):
            continue  # a template row left unused
        try:
            missing = [c for c in METER_REQUIRED if not r.get(c)]
            if missing:
                raise ValueError(f"Fill in {', '.join(missing)}.")
            station = lookup.station(r["station"])
            day = _date(r["date"])
            start = _when(day, _time(r["start_time"]))
            end = _when(day, _time(r["end_time"]))
            if end <= start:
                end += timedelta(days=1)  # a night shift ends the next morning
            if end > now:
                raise ValueError("This shift has not ended yet. Only past shifts can be imported.")
            people = [lookup.attendant(p) for p in r["attendants"].replace(",", ";").split(";") if p.strip()]
            nozzle = lookup.nozzle(station, r["pump"], r["nozzle"])
            opening = _number(r["opening_meter"], "Opening meter", decimal_comma)
            closing = _number(r["closing_meter"], "Closing meter", decimal_comma)
            price = _number(r["price_per_litre"], "Price", decimal_comma) if r.get("price_per_litre") else None
            if price == 0:
                raise ValueError("Price must be more than zero.")
            holder = lookup.attendant(r["pump_attendant"]) if r.get("pump_attendant") else None
        except ValueError as exc:
            errors.add("meters", number, str(exc))
            continue
        flags = []
        dispensed_litres(opening, closing, nozzle.meter_max, flags)
        if "meter_went_backwards" in flags:
            errors.add("meters", number, f"Closing meter {closing} is below opening meter {opening}.")
            continue
        key = (station.pk, start)
        shift = shifts.get(key)
        if shift is None:
            shift = shifts[key] = _Shift(station=station, start=start, end=end, people=people, first_row=number)
        elif shift.end != end or {u.pk for u in shift.people} != {u.pk for u in people}:
            errors.add("meters", number, f"This shift (row {shift.first_row}) has a different end time or attendants here. Every row of one shift must match.")
            continue
        if any(n.pk == nozzle.pk for _, n, *_ in shift.readings):
            errors.add("meters", number, f"{nozzle.pump.name} nozzle {nozzle.number} appears twice in this shift.")
            continue
        shift.readings.append((number, nozzle, opening, closing, price, holder))
    return shifts


def _check_shifts(shifts, errors):
    by_station = {}
    for s in shifts.values():
        row = s.first_row
        if not s.people:
            errors.add("meters", row, "Name who worked this shift (attendants).")
        elif len(s.people) > 1 and s.station.attendants_mode != Station.AttendantsMode.MULTIPLE:
            errors.add("meters", row, f"{s.station.name} has one attendant per shift.")
        assigned = s.station.assignment_mode == Station.AssignmentMode.ASSIGNED
        holders = {}
        for number, nozzle, *_, holder in s.readings:
            if holder is None and assigned:
                if len(s.people) != 1:
                    errors.add("meters", number, f"{s.station.name} has assigned pumps: say who held {nozzle.pump.name} (pump_attendant).")
                    continue
                holder = s.people[0]
            if holder is not None and holder.pk not in {u.pk for u in s.people}:
                errors.add("meters", number, f"{holder.full_name} is not one of this shift's attendants.")
            elif holder is not None and holders.setdefault(nozzle.pump_id, holder).pk != holder.pk:
                errors.add("meters", number, f"{nozzle.pump.name} has two different pump attendants in this shift.")
        s.holders = holders if assigned else {}
        by_station.setdefault(s.station.pk, []).append(s)
        clash = (Shift.objects.filter(station=s.station, started_at__lt=s.end).filter(Q(ended_at__gt=s.start) | Q(ended_at__isnull=True))
                 .order_by("started_at").first())
        if clash:
            errors.add("meters", row, f"{s.station.name} already has a shift from {_shown(clash.started_at)} in the system. This one overlaps it (was it imported before?).")
    for rows in by_station.values():
        rows.sort(key=lambda s: s.start)
        for a, b in zip(rows, rows[1:], strict=False):
            if b.start < a.end:
                errors.add("meters", b.first_row, f"This shift starts before the shift in row {a.first_row} ends.")


def _parse_cash(rows, decimal_comma, lookup, shifts, errors):
    seen = set()
    for number, r in rows:
        try:
            missing = [c for c in CASH_REQUIRED if not r.get(c)]
            if missing:
                raise ValueError(f"Fill in {', '.join(missing)}.")
            station = lookup.station(r["station"])
            start = _when(_date(r["date"]), _time(r["start_time"]))
            shift = shifts.get((station.pk, start))
            if shift is None:
                raise ValueError(f"No shift at {station.name} starting {_shown(start)} in the meters file.")
            person = lookup.attendant(r["attendant"])
            if person.pk not in {u.pk for u in shift.people}:
                raise ValueError(f"{person.full_name} is not one of this shift's attendants.")
            kind = PAYMENT_TYPES.get(r["payment_type"].strip().lower())
            if kind is None:
                raise ValueError(f"Payment type '{r['payment_type']}' is not known. Use cash, ecocash, card or credit.")
            currency = (r.get("currency") or station.pricing_currency).upper()
            if len(currency) != 3 or not currency.isalpha():
                raise ValueError(f"Currency '{currency}' should be a 3-letter code such as USD or ZWG.")
            amount = _number(r["amount"], "Amount", decimal_comma)
        except ValueError as exc:
            errors.add("cash", number, str(exc))
            continue
        if (key := (shift.station.pk, shift.start, person.pk, kind, currency)) in seen:
            errors.add("cash", number, f"{person.full_name} already has a {kind} {currency} amount for this shift.")
            continue
        seen.add(key)
        shift.cash.append((number, person, kind, currency, amount))


def _plan_prices(shifts, errors):
    """Check each shift against price history and plan the old prices that fill its gaps. Returns rows to create."""
    points = {}  # series -> [(start, end, price, row)]
    for s in shifts.values():
        # A price written on one nozzle row counts for the shift's other nozzles of that fuel.
        given = {n.tank.fuel_type_id: price for _, n, _, _, price, _ in s.readings if price is not None}
        for number, nozzle, _, _, price, _ in s.readings:
            series = (s.station, nozzle.tank.fuel_type, s.station.pricing_currency)
            points.setdefault(series, []).append((s.start, s.end, price if price is not None else given.get(nozzle.tank.fuel_type_id), number))
    planned = []
    for (station, fuel, currency), items in points.items():
        history = list(FuelPrice.objects.filter(station=station, fuel_type=fuel, currency=currency).order_by("effective_from"))
        gaps = {}
        for start, end, price, number in items:
            current = next((p for p in history if p.effective_from <= start and (p.effective_to is None or p.effective_to > start)), None)
            if current is not None:
                if price is not None and price != current.price_per_litre:
                    errors.add("meters", number, f"{fuel.name} at {station.name} was {_fmt(current.price_per_litre)} {currency} on {_shown(start)} in the system, but the file says {_fmt(price)}.")
                continue
            if price is None:
                errors.add("meters", number, f"No {fuel.name} price at {station.name} on {_shown(start)}. Add price_per_litre to this row.")
                continue
            gap_end = min((p.effective_from for p in history if p.effective_from > start), default=None)
            gaps.setdefault(gap_end, []).append((start, end, price, number))
        for gap_end, rows in gaps.items():
            rows.sort()
            runs = []  # [first start, last end, price]
            for start, end, price, number in rows:
                if runs and runs[-1][0] == start and runs[-1][2] != price:
                    errors.add("meters", number, f"Two different {fuel.name} prices for the same shift.")
                elif runs and runs[-1][2] == price:
                    runs[-1][1] = max(runs[-1][1], end)
                else:
                    runs.append([start, end, price])
            for i, (start, end, price) in enumerate(runs):
                until = runs[i + 1][0] if i + 1 < len(runs) else (gap_end or end)
                planned.append({"station": station, "fuel": fuel, "currency": currency, "price": price, "from": start, "to": until})
    return planned


# ---- writing -------------------------------------------------------------------------------------------------------

def _write_shift(s, user, note):
    shift = Shift.objects.create(tenant_id=user.tenant_id, station=s.station, started_at=s.start, ended_at=s.end, status=Shift.Status.CLOSED,
                                 close_requested_at=s.end, closed_by=user, notes=note)
    handed_in = {person.pk for _, person, *_ in s.cash}
    for i, person in enumerate(s.people):
        ShiftAttendant.objects.create(tenant_id=user.tenant_id, shift=shift, user=person, is_lead=i == 0, joined_at=s.start,
                                      cash_submitted_at=s.end if person.pk in handed_in else None)
    for _, nozzle, opening, closing, *_ in s.readings:
        for kind, reading, at in ((PumpMeterReading.Kind.START, opening, s.start), (PumpMeterReading.Kind.END, closing, s.end)):
            PumpMeterReading.objects.create(tenant_id=user.tenant_id, shift=shift, nozzle=nozzle, kind=kind, reading=reading.quantize(Decimal("0.01")),
                                            reading_at=at, source="import", recorded_by=user)
    for pump_id, person in s.holders.items():
        PumpAssignment.objects.create(tenant_id=user.tenant_id, shift=shift, pump_id=pump_id, user=person, started_at=s.start, ended_at=s.end, assigned_by=user)
    for _, person, kind, currency, amount in s.cash:
        CashSubmission.objects.create(tenant_id=user.tenant_id, shift=shift, attendant=person, payment_type=kind, currency=currency, amount=amount.quantize(Decimal("0.01")))
    compute_transactions(shift)
    on_shift_closed(shift)
    return shift


def import_shifts(*, user, meters, cash=None, commit=False, request=None):
    """Check the files and, with commit, save every shift in them (all or nothing). Returns a summary either way."""
    errors = _Errors()
    meter_rows, meter_comma = read_csv(meters, "meters", METER_REQUIRED)
    if len(meter_rows) > MAX_METER_ROWS:
        raise ValidationError({"errors": [{"file": "meters", "row": None, "message": f"More than {MAX_METER_ROWS} rows. Import a few months at a time."}]})
    cash_rows, cash_comma = read_csv(cash, "cash", CASH_REQUIRED) if cash else ([], False)
    lookup = _Lookup()
    shifts = _parse_meters(meter_rows, meter_comma, lookup, errors)
    if not shifts and not errors.items:
        errors.add("meters", None, "No shifts found in this file.")
    _check_shifts(shifts, errors)
    _parse_cash(cash_rows, cash_comma, lookup, shifts, errors)
    prices = _plan_prices(shifts, errors)
    errors.raise_if_any()

    name = getattr(meters, "name", "meters.csv")
    note = f"Imported from {name} by {user.full_name} on {timezone.localdate():%d %b %Y}."
    summary = {}
    try:
        with db_tx.atomic(), deferred():
            for p in prices:
                FuelPrice.objects.create(tenant_id=user.tenant_id, station=p["station"], fuel_type=p["fuel"], currency=p["currency"],
                                         price_per_litre=p["price"], effective_from=p["from"], effective_to=p["to"], created_by=user)
            ids = [_write_shift(s, user, note).pk for s in sorted(shifts.values(), key=lambda s: s.start)]
            sales = Transaction.objects.filter(shift_id__in=ids).values("currency").annotate(litres=Sum("litres"), amount=Sum("amount")).order_by("currency")
            starts = [s.start for s in shifts.values()]
            summary = {
                "shifts": len(ids), "readings": sum(len(s.readings) for s in shifts.values()) * 2, "cash_rows": sum(len(s.cash) for s in shifts.values()),
                "from": min(starts), "to": max(s.end for s in shifts.values()),
                "stations": sorted({s.station.name for s in shifts.values()}),
                "litres": str(sum((r["litres"] for r in sales), Decimal(0))),
                "amount_by_currency": {r["currency"]: str(r["amount"]) for r in sales},
                "flagged_shifts": Shift.objects.filter(pk__in=ids).exclude(flags=[]).count(),
                "prices_added": [{"station": p["station"].name, "fuel": p["fuel"].name, "currency": p["currency"], "price_per_litre": str(p["price"]),
                                  "from": p["from"], "to": p["to"]} for p in prices],
                "saved": commit,
            }
            if not commit:
                raise _DryRun
            audit.log("shifts_imported", actor=user, request=request, changes={
                "file": name, "shifts": len(ids), "from": summary["from"].isoformat(), "to": summary["to"].isoformat(),
                "stations": summary["stations"], "prices_added": len(prices)})
    except _DryRun:
        pass
    return summary


def template_csv(kind):
    """A CSV to fill in: the meters template lists every nozzle of every station, so only numbers need typing."""
    out = io.StringIO()
    writer = csv.writer(out)
    if kind == "cash":
        writer.writerow(CASH_COLUMNS)
        return out.getvalue()
    writer.writerow(METER_COLUMNS)
    for n in Nozzle.objects.select_related("pump__station", "tank__fuel_type").order_by("pump__station__name", "pump__name", "number"):
        writer.writerow([n.pump.station.name, "", "", "", "", n.pump.name, n.number, "", "", "", ""])
    return out.getvalue()
