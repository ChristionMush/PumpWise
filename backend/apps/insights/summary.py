"""The owner's daily message: so they only open the app when something is wrong."""
from datetime import date

from django.utils import timezone

from apps.reports.reconciliation import tank_range
from apps.reports.sales import daily_sales
from apps.stations.models import Station, Tank
from apps.tenants.models import Tenant
from apps.tenants.services import get_settings

from .forecast import forecast_tank
from .models import Alert


def build_daily_summary(tenant_id, day: date | None = None):
    day = day or timezone.localdate()
    tenant = Tenant.objects.get(pk=tenant_id)
    cfg = get_settings(tenant_id)
    name = cfg.display_name or tenant.name
    lines = [f"{name}: {day:%d %b}"]
    total_l, totals = 0, {}
    for st in Station.objects.filter(status="active"):
        d = daily_sales(station_ids=[st.pk], date_from=day, date_to=day)
        litres = float(d["totals"]["litres"])
        total_l += litres
        for c, a in d["totals"]["amount_by_currency"].items():
            totals[c] = totals.get(c, 0) + float(a)
        lines.append(f"{st.name}: {litres:,.0f} L" + "".join(f" {c} {float(a):,.2f}" for c, a in d["totals"]["amount_by_currency"].items()))
    lines.insert(1, f"Sold {total_l:,.0f} L" + "".join(f" / {c} {a:,.2f}" for c, a in totals.items()))
    problems = []
    tanks = list(Tank.objects.select_related("fuel_type", "station"))
    for t in tanks:
        f = forecast_tank(t, cfg)
        if f["status"] == "ok" and f["low"]:
            problems.append(f"LOW {t.station.name} {t.name}: {f['days_to_empty'] if f['days_to_empty'] is not None else '?'} days left, order by {f['refill_by'] or 'now'}")
    for r in tank_range(tanks, day, day):
        if r["flagged"]:
            problems.append(f"{'LOSS' if r['status'] == 'loss' else 'CHECK'} {r['tank']}: {r['loss']} L unexplained")
    for a in Alert.objects.filter(status="open", kind="cash"):
        problems.append(a.message)
    lines.append("All good." if not problems else "Needs attention:")
    lines.extend(problems[:6])
    from .alert_actions import summary_lines

    lines.extend(summary_lines(day))  # what managers resolved today and how, and what is still open
    return "\n".join(lines)
