"""Presentation companies: demo businesses for showing future clients (Tenant.demo_profile = a key of COMPANIES).

* "presentation", Savanna Fuels (made up): 2 stations. Samora Machel Avenue (Harare) has assigned pumps, two attendants
  per shift; Bulawayo CBD shares its pumps, one attendant per shift.
* "litrix", Litrix: its own Cowdray Park Station and its own people (Rose the manager, Stewart on days, Thabani on
  nights, both on all four pumps). Its logins are kept: the loader finds each person by name and role.

Each station has a manager, a day team (07:00-18:00) and a night team (18:00-07:00). On assigned pumps the manager
allocates every pump before each shift. The manager closes each day a little after 21:00 (every tank dipped, every
pump's meter read), approves every end of shift, and orders fuel before a tank runs low.

What it shows, all through the real services so every screen, report and alert behaves as for a real customer:
* mostly smooth days: normal sales, cash within $1 (attendants hand in whole dollars), deliveries, every day closed;
* problems caught: three cash shortages, a fuel loss on one tank, a tank running low, a pump with nobody on it for a
  night;
* approvals in action: every end approved, one sent back and fixed (the EcoCash total was missing), one change asked
  for, allowed and made (swipe money put under cash).

The data is made for the moment it is loaded (`now`): everything before now happened, the shift working now is open.
Each event is replayed at its own time (the clock is set to it), so approvals, the audit trail and the day log show
when things happened, not when the demo was loaded.
"""
import random
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from unittest import mock

from django.utils import timezone

D = Decimal
PROFILE = "presentation"  # data["profile"]: replayed by load() below
NAME = "Savanna Fuels"
ZWG_RATE = D("0.037")
WEEKDAY_FACTOR = {4: 1.2, 5: 1.35, 6: 0.85}  # Fri, Sat busy; Sun quiet
NIGHT_FACTOR, BEFORE_CLOSE = 0.55, D("0.35")  # night sells about half a day shift; a third of it before the 21:00 close
FOUR = ["Pump 1", "Pump 2", "Pump 3", "Pump 4"]
LOW_FROM = 10

# Stories, by the day before the load (0 = yesterday), the shift, the station and who:
# short: handed in that much too little; loss: litres missing at that day's close; low: the tank's last delivery is
# LOW_FROM days ago, sized to run low by now; nobody: that pump not allocated for the shift; sent_back: handed in
# without the EcoCash, sent back, fixed, approved; change: put the swipe money under cash, asks to change it, allowed.
COMPANIES = {
    "presentation": {
        "name": NAME, "summary_phone": "+263770000002", "supplier": ("Kariba Fuel Supplies", "KFS"), "price_change": None,
        "prices": {"PETROL": ("1.52", "1.58"), "DIESEL": ("1.55", "1.60")},
        "stations": [
            {"key": "sam", "name": "Samora Machel Avenue", "address": "Samora Machel Avenue, Harare", "size": 1.3, "assigned": True, "pumps": FOUR,
             "tanks": {"PETROL": ("Petrol tank", 35000, 1500, True), "DIESEL": ("Diesel tank", 30000, 1500, True)},
             "teams": {"day": {"d1": [0, 1], "d2": [2, 3]}, "night": {"n1": [0, 1], "n2": [2, 3]}},
             "staff": {"mgr": "Tendai Dube", "d1": "Rutendo Marufu", "d2": "Farai Hove", "n1": "Nyasha Muzenda", "n2": "Tatenda Nyoni"}},
            {"key": "byo", "name": "Bulawayo CBD", "address": "Bulawayo CBD, Bulawayo", "size": 1.1, "assigned": False, "pumps": FOUR[:3],
             "tanks": {"PETROL": ("Petrol tank", 35000, 1500, True), "DIESEL": ("Diesel tank", 30000, 1500, True)},
             "teams": {"day": {"d1": [0, 1, 2]}, "night": {"n1": [0, 1, 2]}},
             "staff": {"mgr": "Chipo Sibanda", "d1": "Kudakwashe Phiri", "n1": "Tafadzwa Musoni"}},
        ],
        "stories": {"short": {(1, "day", "sam", "d2"): "23.50", (2, "night", "byo", "n1"): "41.00", (0, "day", "byo", "d1"): "12.20"},
                    "loss": (1, "byo", "PETROL", 310), "low": "byo-DIESEL", "nobody": (1, "night", "sam", 3),
                    "sent_back": (2, "day", "byo", "d1"), "change": (1, "night", "sam", "n1")},
    },
    "litrix": {
        "name": "Litrix", "summary_phone": None, "supplier": ("Kariba Fuel Supplies", "KFS"),
        "price_change": (2026, 10, 16, 18, 13),  # Litrix's own price change (16 Oct), between the shifts
        "prices": {"PETROL": ("1.45", "1.48"), "DIESEL": ("1.52", "1.50")},
        "stations": [
            {"key": "cow", "name": "Cowdray Park Station", "address": "2930 Scara Street.", "size": 1.0, "assigned": True,
             "pumps": ["Pump 1a", "Pump 1b", "Pump 2a", "Pump 2b"], "make": ("Gilbarco", "Encore 300", 2016),
             "tanks": {"PETROL": ("Tank 1", 20000, 2000, False), "DIESEL": ("Tank 2", 30000, 3000, False)},
             "teams": {"day": {"d1": [0, 1, 2, 3]}, "night": {"n1": [0, 1, 2, 3]}},
             "staff": {"mgr": "Rose", "d1": "Stewart", "n1": "Thabani"}},
        ],
        "stories": {"short": {(1, "day", "cow", "d1"): "23.50", (2, "night", "cow", "n1"): "41.00", (0, "day", "cow", "d1"): "12.20"},
                    "loss": (1, "cow", "PETROL", 310), "low": "cow-DIESEL", "nobody": (1, "night", "cow", 3),
                    "sent_back": (2, "day", "cow", "d1"), "change": (1, "night", "cow", "n1")},
    },
}


def _t(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


def _q(x, places="0.01"):
    return D(x).quantize(D(places))


def cash_lines(handed, *, variant=None):
    """What one person hands in: EcoCash 25%, swipe 10%, ZiG 5%, the rest US dollars cash."""
    eco, card = _q(handed * D("0.25")), _q(handed * D("0.10"))
    zwg = _q(handed * D("0.05") / ZWG_RATE, "1")
    usd = _q(handed - eco - card - zwg * ZWG_RATE)
    lines = {("cash", "USD"): usd, ("cash", "ZWG"): zwg, ("mobile_money", "USD"): eco, ("card", "USD"): card}
    if variant == "no_ecocash":
        lines[("mobile_money", "USD")] = D(0)
    elif variant == "swipe_as_cash":
        lines[("cash", "USD")], lines[("card", "USD")] = usd + card, D(0)
    return [{"payment_type": k[0], "currency": k[1], "amount": str(v)} for k, v in lines.items() if v or variant]


def generate(*, now=None, days=30, seed=11, name=None, company=PROFILE):
    """A presentation company's data up to `now` (local time), as plain JSON values."""
    co = COMPANIES[company]
    stories = co["stories"]
    short_by = {k: D(v) for k, v in stories["short"].items()}
    now = timezone.localtime(now or timezone.now()).replace(tzinfo=None, second=0, microsecond=0)
    rnd = random.Random(seed)
    end = now.date() - timedelta(days=1)  # the last full day
    first = end - timedelta(days=days - 1)
    rise_at = (datetime(*co["price_change"]) if co["price_change"]
               else datetime.combine(end - timedelta(days=11), datetime.min.time()) + timedelta(hours=18, minutes=13))  # between the shifts
    prices_by = {code: (D(old), D(new)) for code, (old, new) in co["prices"].items()}
    staff, stations, state, events = [], [], {}, []

    def ev(at, **kw):
        if at < now:
            events.append({"at": _t(at), **kw})
            return True
        return False

    for spec in co["stations"]:
        key, assigned = spec["key"], spec["assigned"]
        team = {slot: list(spec["teams"][slot]) for slot in ("day", "night")}
        for who, full_name in spec["staff"].items():
            staff.append({"key": f"{key}-{who}", "role": "manager" if who == "mgr" else "attendant", "full_name": full_name, "station": key})
        tanks = [{"key": f"{key}-{code}", "name": tname, "fuel": code, "capacity": str(cap), "reserve": str(reserve),
                  "calibration": [[0, 0], [500, int(cap * 0.3)], [1000, int(cap * 0.66)], [1500, cap]] if chart else []}
                 for code, (tname, cap, reserve, chart) in spec["tanks"].items()]
        pumps, meters = [], {}
        for i, pname in enumerate(spec["pumps"]):
            pkey = f"{key}-p{i + 1}"
            nozzles = []
            for number, code in ((1, "PETROL"), (2, "DIESEL")):
                meters[f"{pkey}:{number}"] = D(rnd.randint(150000, 900000)) + _q(rnd.random())
                nozzles.append({"number": number, "tank": f"{key}-{code}"})
            holder = {slot: next(who for who, mine in spec["teams"][slot].items() if i in mine) for slot in ("day", "night")}
            brand, model, year = spec.get("make") or (rnd.choice(["Wayne", "Gilbarco", "Tokheim"]), f"M{rnd.randint(100, 900)}", rnd.randint(2010, 2022))
            pumps.append({"key": pkey, "name": pname, "brand": brand, "model": model, "year": year, "nozzles": nozzles, "holder": holder,
                          **({"assigned_to": f"{key}-{holder['day']}"} if assigned else {})})
        prices = [{"fuel": code, "currency": "USD", "price": str(p), "from": _t(when)}
                  for code, (old, new) in prices_by.items() for p, when in ((old, datetime.combine(first, datetime.min.time()) - timedelta(days=20)), (new, rise_at))]
        stations.append({"key": key, "name": spec["name"], "address": spec["address"], "pricing_currency": "USD",
                         "attendants_mode": "multiple" if assigned else "single", "assignment_mode": "assigned" if assigned else "shared",
                         "tanks": tanks, "pumps": pumps, "prices": prices})
        state[key] = {"size": spec["size"], "assigned": assigned, "team": team, "pumps": pumps, "meters": meters, "sold": {t["key"]: [] for t in tanks},
                      "levels": {f"{key}-{code}": D(t[1]) * D("0.7" if code == "PETROL" else "0.6") for code, t in spec["tanks"].items()},
                      "cap": {code: t[1] for code, t in spec["tanks"].items()}, "reserve": {f"{key}-{code}": D(t[2]) for code, t in spec["tanks"].items()}}

    def price(fuel, at):
        return prices_by[fuel][1 if at >= rise_at else 0]

    day = first
    while datetime.combine(day, datetime.min.time()) + timedelta(hours=7) < now:
        ago = (end - day).days
        factor = WEEKDAY_FACTOR.get(day.weekday(), 1.0)
        for slot in ("day", "night"):
            begin = datetime.combine(day, datetime.min.time()) + timedelta(hours=7 if slot == "day" else 18)
            if begin >= now:
                break
            for key in state:
                st = state[key]
                mgr = f"{key}-mgr"
                if st["assigned"]:  # the manager allocates every pump before the shift
                    for i, p in enumerate(st["pumps"]):
                        if (ago, slot, key, i) != stories["nobody"]:
                            ev(begin - timedelta(minutes=rnd.randint(15, 35)), type="assign", by=mgr, pump=p["key"], to=f"{key}-{p['holder'][slot]}")
                close_at = datetime.combine(day, datetime.min.time()) + timedelta(hours=21, minutes=rnd.randint(5, 30)) if slot == "night" else None
                before_close = {}
                for who in st["team"][slot]:
                    person = f"{key}-{who}"
                    mine = [p for i, p in enumerate(st["pumps"]) if p["holder"][slot] == who and (ago, slot, key, i) != stories["nobody"]]
                    start = begin + timedelta(minutes=rnd.randint(15, 40) if slot == "day" else rnd.randint(15, 35))
                    nks = [f"{p['key']}:{n['number']}" for p in mine for n in p["nozzles"]]
                    if not ev(start, type="start", station=key, who=person, readings={nk: str(st["meters"][nk]) for nk in nks}):
                        continue
                    expected, ends = D(0), {}
                    for p in mine:
                        for n in p["nozzles"]:
                            nk, fuel = f"{p['key']}:{n['number']}", n["tank"].split("-")[1]
                            base = rnd.uniform(300, 450) if fuel == "PETROL" else rnd.uniform(200, 320)
                            litres = _q(base * st["size"] * factor * (NIGHT_FACTOR if slot == "night" else 1) * rnd.uniform(0.85, 1.15))
                            if close_at is not None:
                                before_close[nk] = (st["meters"][nk], _q(litres * BEFORE_CLOSE))
                            st["meters"][nk] += litres
                            ends[nk] = str(st["meters"][nk])
                            st["sold"][n["tank"]].append((slot, litres))
                            expected += litres * price(fuel, start)
                    stop = (datetime.combine(day, datetime.min.time()) + timedelta(hours=18, minutes=rnd.randint(-4, 12)) if slot == "day"
                            else datetime.combine(day + timedelta(days=1), datetime.min.time()) + timedelta(hours=7, minutes=rnd.randint(-6, 10)))
                    short = short_by.get((ago, slot, key, who), D(0))
                    handed = (expected.quantize(D(1), ROUND_FLOOR if rnd.random() < 0.5 else ROUND_HALF_UP)) - short
                    story = (ago, slot, key, who)
                    variant = "no_ecocash" if story == stories["sent_back"] else "swipe_as_cash" if story == stories["change"] else None
                    if not ev(stop, type="end", who=person, readings=ends, cash=cash_lines(handed, variant=variant)):
                        continue
                    if story == stories["sent_back"]:
                        ev(stop + timedelta(minutes=30), type="send_back", by=mgr, who=person, note="Your EcoCash total is missing. Add it and send again.")
                        ev(stop + timedelta(minutes=48), type="fix", who=person, cash=cash_lines(handed))
                        ev(stop + timedelta(minutes=70), type="approve", by=mgr, who=person)
                    elif story == stories["change"]:
                        ev(stop + timedelta(minutes=12), type="ask", who=person, kind="cash", reason="I put the swipe money under cash.")
                        ev(stop + timedelta(minutes=35), type="allow", by=mgr, who=person, note="OK, put it under swipe.")
                        ev(stop + timedelta(minutes=44), type="fix", who=person, cash=cash_lines(handed))
                        ev(stop + timedelta(minutes=65), type="approve", by=mgr, who=person)
                    else:
                        ev(stop + timedelta(minutes=rnd.randint(20, 75)), type="approve", by=mgr, who=person)
                if close_at is None:  # the day shift's sales come off the tanks at the evening close
                    continue
                # the day close: every tank dipped and every pump read, the night shift a third of the way through
                meters_at_close = {nk: str(st["meters"][nk]) for nk in st["meters"]}
                for nk, (start_meter, litres) in before_close.items():
                    meters_at_close[nk] = str(start_meter + litres)
                levels = {}
                for t in st["levels"]:
                    today_sold = sum((litres for s, litres in st["sold"][t] if s == "day"), D(0))
                    night_before = sum((litres for nk, (_, litres) in before_close.items() if nk.split(":")[1] == ("1" if t.endswith("PETROL") else "2")), D(0))
                    st["levels"][t] -= today_sold + night_before
                    if (ago, key, t.split("-")[1]) == stories["loss"][:3]:
                        st["levels"][t] -= stories["loss"][3]
                    levels[t] = st["levels"][t]
                    st["sold"][t] = [("night_rest", sum((litres for s, litres in st["sold"][t] if s == "night"), D(0)) - night_before)]
                if ev(close_at, type="close", station=key, by=mgr, dips={t: str(_q(v + D(str(round(rnd.uniform(-8, 8), 1))), "0.1")) for t, v in levels.items()},
                      meters=meters_at_close):
                    for t in st["levels"]:  # the rest of the night's sales, then a delivery the next morning if the tank is getting low
                        st["levels"][t] -= st["sold"][t][0][1]
                        st["sold"][t] = []
                        daily = _daily_use(st, t)
                        deliver = None
                        if t == stories["low"] and ago == LOW_FROM:
                            deliver = _thousands(daily * D(13) + st["reserve"][t] - st["levels"][t])
                        elif not (t == stories["low"] and ago < LOW_FROM) and st["levels"][t] < st["reserve"][t] + daily * 5:
                            deliver = _thousands(st["cap"][t.split("-")[1]] * D("0.9") - st["levels"][t])
                        if deliver:
                            at = datetime.combine(day + timedelta(days=1), datetime.min.time()) + timedelta(hours=10, minutes=rnd.randint(0, 90))
                            if ev(at, type="delivery", by=mgr, tank=t, litres=str(deliver), supplier=co["supplier"][0], note_number=f"{co['supplier'][1]}-{rnd.randint(10000, 99999)}"):
                                st["levels"][t] += deliver
                else:
                    for t in st["levels"]:
                        st["sold"][t] = []
        day += timedelta(days=1)

    events.sort(key=lambda e: e["at"])  # by time; the same minute keeps the order things were done in
    return {"profile": PROFILE, "company": company, "end": end.isoformat(), "now": _t(now), "days": days, "seed": seed,
            "business": {"name": name or co["name"], "summary_phone": co["summary_phone"]}, "staff": staff,
            "exchange_rates": [{"from_currency": "ZWG", "to_currency": "USD", "rate": str(ZWG_RATE), "effective_from": _t(datetime.combine(first, datetime.min.time()) - timedelta(days=20))}],
            "stations": stations, "events": events}


def _thousands(litres):
    """A tanker load: whole thousands of litres, never negative."""
    return max(D(0), (D(litres) / 1000).quantize(D(1), ROUND_HALF_UP) * 1000)


def _daily_use(st, tank):
    """About how much a tank sells a day at this station (for ordering)."""
    fuel = tank.split("-")[1]
    base = 375 if fuel == "PETROL" else 260
    return D(base * st["size"] * len(st["pumps"]) * (1 + NIGHT_FACTOR))


# ---- loading -------------------------------------------------------------------------------------------------------

@contextmanager
def _clock(at):
    """Run the services as if it were `at` (record times, approvals, the audit trail)."""
    with mock.patch("django.utils.timezone.now", return_value=at):
        yield


def load(tenant, data, *, password=None):
    """Replay the presentation data into a demo business through the real services. Returns the staff logins."""
    from apps.accounts.models import Role, User
    from apps.accounts.utils import generate_temporary_password
    from apps.core.db import db_context
    from apps.insights.alerts import evaluate_tenant
    from apps.operations import approvals
    from apps.operations import services as ops
    from apps.operations.dayclose import close_day
    from apps.operations.models import ShiftAttendant, UndoRequest
    from apps.operations.rollups import deferred
    from apps.stations.models import CalibrationPoint, ExchangeRate, FuelType, Nozzle, Pump, Station, StationAssignment, Tank
    from apps.stations.services import set_price
    from apps.tenants.features import set_station_policy
    from apps.tenants.services import DEFAULT_FUEL_TYPES, get_settings

    from .demo import NotDemoTenant, _local

    if not tenant.is_demo:
        raise NotDemoTenant(str(tenant.pk))
    with db_context(tenant_id=tenant.id), deferred():
        owner = User.objects.filter(role=Role.OWNER).order_by("created_at").first()
        cfg = get_settings(tenant.id)
        if data["business"]["summary_phone"]:  # none given: the business keeps its own (Litrix)
            cfg.summary_phone, cfg.summary_channel = data["business"]["summary_phone"], "sms"
            cfg.save()
        fuel = {f.code: f for f in FuelType.objects.all()}
        for fname, code in DEFAULT_FUEL_TYPES:
            if code not in fuel:
                fuel[code] = FuelType.objects.create(tenant_id=tenant.id, name=fname, code=code)
        for r in data["exchange_rates"]:
            ExchangeRate.objects.create(tenant_id=tenant.id, from_currency=r["from_currency"], to_currency=r["to_currency"], rate=D(r["rate"]),
                                        effective_from=_local(r["effective_from"]), created_by=owner)
        stations, tanks, pumps, nozzles = {}, {}, {}, {}
        for s in data["stations"]:
            st = Station.objects.create(tenant_id=tenant.id, name=s["name"], address=s["address"], pricing_currency=s["pricing_currency"])
            with db_context(platform_admin=True):
                set_station_policy(station=st, attendants_mode=s["attendants_mode"], assignment_mode=s["assignment_mode"])
            st.refresh_from_db()
            stations[s["key"]] = st
            for t in s["tanks"]:
                tank = Tank.objects.create(tenant_id=tenant.id, station=st, name=t["name"], fuel_type=fuel[t["fuel"]], capacity_litres=D(t["capacity"]), reserve_litres=D(t["reserve"]))
                for dip_mm, litres in t["calibration"]:
                    CalibrationPoint.objects.create(tenant_id=tenant.id, tank=tank, dip_mm=dip_mm, litres=litres)
                tanks[t["key"]] = tank
            for pr in s["prices"]:
                set_price(tenant_id=tenant.id, station=st, fuel_type=fuel[pr["fuel"]], currency=pr["currency"], price=D(pr["price"]), effective_from=_local(pr["from"]), user=owner)
        staff = {}
        for s in data["staff"]:
            user = User.objects.filter(role=s["role"], full_name=s["full_name"]).first()
            if user is None:
                user = User.objects.create_user(tenant_id=tenant.id, role=s["role"], full_name=s["full_name"], email=f"{s['key']}.{tenant.slug}@demo.example",
                                                password=password or generate_temporary_password())
            staff[s["key"]] = user
            StationAssignment.objects.create(tenant_id=tenant.id, user=user, station=stations[s["station"]])
        for s in data["stations"]:
            for p in s["pumps"]:
                pump = Pump.objects.create(tenant_id=tenant.id, station=stations[s["key"]], name=p["name"], brand=p["brand"], model=p["model"], year=p["year"],
                                           assigned_to=staff[p["assigned_to"]] if p.get("assigned_to") else None)
                pumps[p["key"]] = pump
                for n in p["nozzles"]:
                    nozzles[f"{p['key']}:{n['number']}"] = Nozzle.objects.create(tenant_id=tenant.id, pump=pump, tank=tanks[n["tank"]], number=n["number"],
                                                                                 label=tanks[n["tank"]].fuel_type.name)

        def readings(values):
            return [{"nozzle": nozzles[k], "reading": D(v)} for k, v in values.items()]

        def last_part(user):
            return ShiftAttendant.objects.filter(user=user).select_related("shift", "user").order_by("-joined_at").first()

        for e in data["events"]:
            at = _local(e["at"])
            with _clock(at):
                kind = e["type"]
                if kind == "assign":
                    ops.assign_pump(user=staff[e["by"]], pump=Pump.objects.get(pk=pumps[e["pump"]].pk), to_user=staff[e["to"]], at=at)
                elif kind == "start":
                    ops.start_shift(user=staff[e["who"]], station=stations[e["station"]], started_at=at, readings=readings(e["readings"]))
                elif kind == "end":
                    row = ShiftAttendant.objects.filter(user=staff[e["who"]], left_at__isnull=True).select_related("shift").latest("joined_at")
                    ops.end_shift(user=staff[e["who"]], shift=row.shift, readings=readings(e["readings"]), cash=e["cash"], ended_at=at)
                elif kind == "approve":
                    row = last_part(staff[e["who"]])  # a flagged end is approved with the manager's reason, as in the app
                    approvals.approve(user=staff[e["by"]], row=row, note="Counted with the attendant: approved as handed in" if approvals.end_summary(row)["checks"] else "")
                elif kind == "send_back":
                    approvals.send_back(user=staff[e["by"]], row=last_part(staff[e["who"]]), note=e["note"])
                elif kind == "fix":
                    approvals.fix_end(user=staff[e["who"]], row=last_part(staff[e["who"]]), cash=e["cash"])
                elif kind == "ask":
                    approvals.request_undo(user=staff[e["who"]], row=last_part(staff[e["who"]]), kind=e["kind"], reason=e["reason"])
                elif kind == "allow":
                    req = UndoRequest.objects.filter(attendant__user=staff[e["who"]], status=UndoRequest.Status.PENDING).latest("created_at")
                    approvals.decide_undo(user=staff[e["by"]], req=req, allow=True, note=e["note"])
                elif kind == "close":
                    close_day(user=staff[e["by"]], station=stations[e["station"]], at=at, dips=[{"tank": tanks[k], "litres": D(v)} for k, v in e["dips"].items()],
                              meters=readings(e["meters"]))
                elif kind == "delivery":
                    ops.record_delivery(user=staff[e["by"]], tank=tanks[e["tank"]], litres=D(e["litres"]), delivered_at=at, supplier=e["supplier"], note_number=e["note_number"])
        evaluate_tenant(tenant.id)
    return staff
