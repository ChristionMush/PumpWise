from django.db import migrations

from apps.core.rls import enable_rls, tenant_isolation


class Migration(migrations.Migration):
    """Row-Level Security for the tables created just before (kept separate so every column exists first)."""

    dependencies = [('core', '0001_initial'), ('stations', '0002_station_pricing_currency_fueltype_fuelprice_pump_and_more')]

    operations = [
        enable_rls("stations_fueltype", tenant_isolation()),
        enable_rls("stations_tank", tenant_isolation()),
        enable_rls("stations_calibrationpoint", tenant_isolation()),
        enable_rls("stations_pump", tenant_isolation()),
        enable_rls("stations_nozzle", tenant_isolation()),
        enable_rls("stations_fuelprice", tenant_isolation()),
        enable_rls("stations_exchangerate", tenant_isolation()),
        enable_rls("stations_stationassignment", tenant_isolation()),
    ]
