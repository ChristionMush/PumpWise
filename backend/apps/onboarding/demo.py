"""A believable demo business for sales presentations, staging, and tests. Deterministic for a given seed.

It is built through the real services (shifts, readings, deliveries, dips, prices), never by writing derived
numbers directly, so every screen, report and alert behaves exactly as it will for a real customer.
Planted on purpose: a fuel-loss day (yesterday, station 1 petrol) and a cash difference of -4% of that shift's sales (2 days ago, station 1).
Station 1 runs one attendant per shift with shared pumps; station 2 runs two attendants with assigned pumps (the paid features).
"""
import random
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.utils import timezone

from apps.accounts.models import Role, User
from apps.core.db import db_context
from apps.insights.alerts import evaluate_tenant
from apps.operations import services as ops
from apps.operations.rollups import deferred
from apps.stations.models import CalibrationPoint, ExchangeRate, FuelType, Nozzle, Pump, Station, Tank
from apps.stations.services import price_at, set_price
from apps.tenants.features import set_station_policy, set_tenant_features
from apps.tenants.models import Subscription
from apps.tenants.services import create_tenant_with_owner, get_settings

LOSS_DAYS_AGO, CASH_DAYS_AGO = 1, 2  # inside the alert look-back window, so a fresh demo shows live alerts
WEEKDAY_FACTOR = {4: 1.25, 5: 1.4, 6: 0.8}  # Fri, Sat busy; Sun quiet
D = Decimal


def _local(day: date, hour: int, minute: int = 0):
    return timezone.make_aware(datetime.combine(day, time(hour, minute)), timezone.get_current_timezone())


def seed_demo_tenant(*, name="Demo Fuel Services", days=30, seed=7, password):
    rnd = random.Random(seed)
    now, today = timezone.now(), timezone.localdate()
    token = uuid.uuid4().hex[:6]
    with db_context(platform_admin=True):
        tenant, owner, _ = create_tenant_with_owner(business_name=name, owner_name="Demo Owner", owner_email=f"owner.{token}@demo.example", stations=[], source="platform")
        Subscription.objects.filter(tenant_id=tenant.id).update(plan="demo", paid_through=today + timedelta(days=365))
        set_tenant_features(tenant=tenant, multi_attendant=True, pump_assignment=True)
    with db_context(tenant_id=tenant.id), deferred():
        owner = User.objects.get(pk=owner.pk)
        owner.set_password(password)
        owner.must_change_password = False
        owner.save()
        manager = User.objects.create_user(tenant_id=tenant.id, role=Role.MANAGER, full_name="Demo Manager", email=f"manager.{token}@demo.example", password=password)
        attendant = User.objects.create_user(tenant_id=tenant.id, role=Role.ATTENDANT, full_name="Demo Attendant", email=f"attendant.{token}@demo.example", phone=None, password=password)
        attendant2 = User.objects.create_user(tenant_id=tenant.id, role=Role.ATTENDANT, full_name="Demo Attendant Two", email=f"attendant2.{token}@demo.example", phone=None, password=password)
        cfg = get_settings(tenant.id)
        cfg.summary_phone, cfg.summary_channel, cfg.onboarding_flags = "+263770000001", "sms", {"qr_printed": True}
        cfg.save()
        fuel = {f.code: f for f in FuelType.objects.all()}
        ExchangeRate.objects.create(tenant_id=tenant.id, from_currency="ZWG", to_currency="USD", rate=D("0.037"), effective_from=now - timedelta(days=days + 20), created_by=owner)

        stations = []
        for idx, sname in enumerate(("Harare CBD", "Borrowdale")):
            st = Station.objects.create(tenant_id=tenant.id, name=sname, address=f"{sname}, Harare", pricing_currency="USD")
            if idx == 1:  # the platform sets the paid policy at setup
                with db_context(platform_admin=True):
                    set_station_policy(station=st, attendants_mode="multiple", assignment_mode="assigned")
                st.refresh_from_db()
            tanks = {}
            for code in ("PETROL", "DIESEL"):
                t = Tank.objects.create(tenant_id=tenant.id, station=st, name=f"{code.title()} tank", fuel_type=fuel[code], capacity_litres=20000, reserve_litres=1500)
                for dip, lit in ((0, 0), (400, 6000), (800, 13500), (1200, 20000)):
                    CalibrationPoint.objects.create(tenant_id=tenant.id, tank=t, dip_mm=dip, litres=lit)
                tanks[code] = t
            pumps = []
            for pname, code in (("Pump 1", "PETROL"), ("Pump 2", "PETROL"), ("Pump 3", "DIESEL")):
                p = Pump.objects.create(tenant_id=tenant.id, station=st, tank=tanks[code], name=pname, brand=rnd.choice(["Wayne", "Gilbarco", "Tokheim"]), model=f"M{rnd.randint(100, 900)}", year=rnd.randint(2008, 2020))
                pumps.append((p, [Nozzle.objects.create(tenant_id=tenant.id, pump=p, number=i) for i in (1, 2)]))
            for code, (old, new) in {"PETROL": ("1.45", "1.52"), "DIESEL": ("1.50", "1.55")}.items():
                set_price(tenant_id=tenant.id, station=st, fuel_type=fuel[code], currency="USD", price=D(old), effective_from=now - timedelta(days=days + 10), user=owner)
                set_price(tenant_id=tenant.id, station=st, fuel_type=fuel[code], currency="USD", price=D(new), effective_from=now - timedelta(days=10), user=owner)
            meters = {n.pk: D(rnd.randint(1000, 50000)) for _, ns in pumps for n in ns}
            levels = {tanks["PETROL"].pk: D(15000), tanks["DIESEL"].pk: D(6500)}  # diesel starts low so a delivery happens on day one
            stations.append({"idx": idx, "st": st, "tanks": tanks, "pumps": pumps, "meters": meters, "levels": levels})

        def cash_lines(handed):
            zwg_part = (handed * D("0.05")).quantize(D("0.01"))
            cash = [
                {"payment_type": "cash", "currency": "USD", "amount": (handed * D("0.55")).quantize(D("0.01"))},
                {"payment_type": "cash", "currency": "ZWG", "amount": (zwg_part / D("0.037")).quantize(D("0.01"))},
                {"payment_type": "mobile_money", "currency": "USD", "amount": (handed * D("0.25")).quantize(D("0.01"))},
                {"payment_type": "card", "currency": "USD", "amount": (handed * D("0.10")).quantize(D("0.01"))},
            ]
            cash.append({"payment_type": "credit", "currency": "USD", "amount": max(handed - sum(c["amount"] * (D("0.037") if c["currency"] == "ZWG" else 1) for c in cash), D(0)).quantize(D("0.01"))})
            return cash

        def run_shift(s, day, start_at, end_at, *, factor=1.0, short_frac=0, close=True):
            st, pumps, meters = s["st"], s["pumps"], s["meters"]
            start = [{"nozzle": n, "reading": meters[n.pk]} for _, ns in pumps for n in ns]
            assigned = st.assignment_mode == "assigned"
            # assigned station: Pumps 1-2 (petrol) with the lead, Pump 3 (diesel) with the second attendant
            holder = {p.pk: (attendant2 if (assigned and p.name == "Pump 3") else attendant) for p, _ in pumps}
            shift = ops.start_shift(user=attendant, station=st, started_at=start_at, readings=start,
                                    attendants=[attendant2] if assigned else None, assignments={str(p): u.pk for p, u in holder.items()} if assigned else None)
            if not close:
                return shift, {}
            litres_by_tank, expected, ends = {}, {}, []
            for p, ns in pumps:
                for n in ns:
                    base = rnd.uniform(250, 450) if p.tank.fuel_type.code == "PETROL" else rnd.uniform(300, 480)
                    lit = D(str(round(base * factor * rnd.uniform(0.85, 1.15), 2)))
                    meters[n.pk] += lit
                    ends.append({"nozzle": n, "reading": meters[n.pk]})
                    litres_by_tank[p.tank_id] = litres_by_tank.get(p.tank_id, D(0)) + lit
                    expected[holder[p.pk]] = expected.get(holder[p.pk], D(0)) + lit * price_at(st.pk, p.tank.fuel_type_id, "USD", start_at).price_per_litre
            hand_in = {}
            for person, amount in expected.items():  # short_frac: a proportional difference is flagged on any weekday
                hand_in[person] = (amount * (D(str(round(rnd.uniform(0.999, 1.001), 4))) - D(short_frac))).quantize(D("0.01"))
            if assigned:  # the second attendant hands in first; the lead's 'end' then closes the shift
                ops.end_shift(user=attendant2, shift=shift, readings=[], cash=cash_lines(hand_in.get(attendant2, D(0))), ended_at=end_at)
            ops.end_shift(user=attendant, shift=shift, readings=ends, cash=cash_lines(hand_in.get(attendant, D(0))), ended_at=end_at)
            return shift, litres_by_tank

        for ago in range(days, 0, -1):
            day = today - timedelta(days=ago)
            factor = WEEKDAY_FACTOR.get(day.weekday(), 1.0)
            for s in stations:
                tanks, levels = s["tanks"], s["levels"]
                for t in tanks.values():
                    ops.record_tank_dip(user=manager, tank=t, litres=(levels[t.pk] + D(str(round(rnd.uniform(-5, 5), 1)))).quantize(D("0.1")), at=_local(day, 5, 30))
                    if levels[t.pk] < 7000:
                        ops.record_delivery(user=manager, tank=t, litres=12000, delivered_at=_local(day, 9), supplier="Zuva Petroleum", note_number=f"ZP-{rnd.randint(10000, 99999)}")
                        levels[t.pk] += 12000
                _, sold = run_shift(s, day, _local(day, 6), _local(day, 14), factor=factor, short_frac=D("0.04") if (s["idx"] == 0 and ago == CASH_DAYS_AGO) else D(0))
                for t in tanks.values():
                    loss = D(380) if (s["idx"] == 0 and ago == LOSS_DAYS_AGO and t.fuel_type.code == "PETROL") else D(str(round(rnd.uniform(0, 0.002) * float(sold.get(t.pk, 0)), 2)))
                    levels[t.pk] = levels[t.pk] - sold.get(t.pk, D(0)) - loss
                    ops.record_tank_dip(user=manager, tank=t, litres=levels[t.pk].quantize(D("0.1")), at=_local(day, 15))

        # today: one station already closed a shift, the other has a shift open right now
        s0, s1 = stations
        for t in s1["tanks"].values():
            ops.record_tank_dip(user=manager, tank=t, litres=s1["levels"][t.pk].quantize(D("0.1")), at=now - timedelta(hours=5, minutes=10))
        _, sold = run_shift(s1, today, now - timedelta(hours=5), now - timedelta(hours=1), factor=0.5)
        for t in s1["tanks"].values():
            s1["levels"][t.pk] -= sold.get(t.pk, D(0))
            ops.record_tank_dip(user=manager, tank=t, litres=s1["levels"][t.pk].quantize(D("0.1")), at=now - timedelta(minutes=50))
        for t in s0["tanks"].values():
            ops.record_tank_dip(user=manager, tank=t, litres=s0["levels"][t.pk].quantize(D("0.1")), at=now - timedelta(hours=1, minutes=5))
        run_shift(s0, today, now - timedelta(hours=1), None, close=False)
        evaluate_tenant(tenant.id)

    from apps.billing.services import apply_payment

    with db_context(platform_admin=True):
        from apps.tenants.models import Tenant

        apply_payment(Tenant.objects.get(pk=tenant.id), provider="demo", event_id=f"demo-{tenant.id}", amount=D("86.00"), currency="USD", months=12)
    return {"tenant_id": str(tenant.id), "slug": tenant.slug, "password": password,
            "logins": {"owner": owner.email, "manager": manager.email, "attendant": attendant.email, "attendant2": attendant2.email}}
