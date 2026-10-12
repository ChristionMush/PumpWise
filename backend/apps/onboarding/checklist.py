"""Is this business ready to go live? Computed from real data, so it can never drift from reality."""
from apps.accounts.models import User
from apps.operations.models import Shift, TankReading
from apps.stations.models import FuelPrice, Nozzle, Pump, Station, Tank
from apps.tenants.services import get_settings

MANUAL_FLAGS = {"qr_printed": "QR codes printed and stuck on every pump and tank"}


def build_checklist(tenant_id):
    cfg = get_settings(tenant_id)
    stations = list(Station.objects.filter(status="active"))
    tanks = list(Tank.objects.select_related("station", "fuel_type"))
    pumps = list(Pump.objects.select_related("tank__fuel_type", "station"))
    nozzle_pumps = set(Nozzle.objects.values_list("pump_id", flat=True))

    uncalibrated = [t.name for t in tanks if t.calibration.count() < 2]
    no_nozzles = [p.name for p in pumps if p.pk not in nozzle_pumps]
    unpriced = sorted({f"{p.station.name}: {p.tank.fuel_type.code}" for p in pumps
                       if not FuelPrice.objects.filter(station=p.station, fuel_type=p.tank.fuel_type, currency=p.station.pricing_currency, effective_to__isnull=True).exists()})
    attendants = User.objects.filter(role="attendant", is_active=True).count()
    undipped = [t.name for t in tanks if not TankReading.objects.filter(tank=t).exists()]
    items = [
        ("stations", "Add the station(s)", bool(stations), f"{len(stations)} added", "/setup"),
        ("tanks", "Add every tank (capacity and reserve level)", bool(tanks) and all(any(t.station_id == s.pk for t in tanks) for s in stations), f"{len(tanks)} tanks", "/setup"),
        ("calibration", "Enter each tank's calibration chart (dip height to litres)", bool(tanks) and not uncalibrated, f"Missing: {', '.join(uncalibrated)}" if uncalibrated else "All tanks done", "/setup"),
        ("pumps", "Add every pump and its nozzles", bool(pumps) and not no_nozzles, f"No nozzles on: {', '.join(no_nozzles)}" if no_nozzles else f"{len(pumps)} pumps", "/setup"),
        ("prices", "Set today's price for every fuel", bool(pumps) and not unpriced, f"No price for: {', '.join(unpriced)}" if unpriced else "All fuels priced", "/prices"),
        ("staff", "Create logins for attendants", attendants > 0, f"{attendants} attendants", "/team"),
        ("qr", MANUAL_FLAGS["qr_printed"], bool(cfg.onboarding_flags.get("qr_printed")), "Print from Setup, then tick this off", "/setup"),
        ("dips", "Record a first dip for every tank", bool(tanks) and not undipped, f"Not dipped yet: {', '.join(undipped)}" if undipped else "All tanks dipped", "/dip"),
        ("shift", "Complete one test shift end to end", Shift.objects.filter(status__in=["closed", "reviewed"]).exists(), "Start and end a shift on a phone", "/shift"),
        ("summary", "Set the phone for the owner's daily message", bool(cfg.summary_phone) and cfg.summary_channel != "none", cfg.summary_phone or "Not set", "/settings"),
    ]
    out = [{"key": k, "title": t, "done": d, "detail": detail, "link": link} for k, t, d, detail, link in items]
    done = sum(1 for i in out if i["done"])
    return {"items": out, "done": done, "total": len(out), "percent": round(done * 100 / len(out)), "ready": done == len(out)}
