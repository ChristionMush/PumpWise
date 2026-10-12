from django.db import migrations

from apps.core.rls import PLATFORM, TENANT, Policy, enable_rls


def platform_only(table):
    return enable_rls(
        table,
        [
            Policy("platform_select", "SELECT", using=PLATFORM),
            Policy("platform_insert", "INSERT", check=PLATFORM),
            Policy("platform_update", "UPDATE", using=PLATFORM, check=PLATFORM),
        ],
    )


def tenant_reads_platform_writes(table):
    return enable_rls(
        table,
        [
            Policy("tenant_select", "SELECT", using=f"tenant_id = {TENANT} OR {PLATFORM}"),
            Policy("platform_insert", "INSERT", check=PLATFORM),
            Policy("platform_update", "UPDATE", using=PLATFORM, check=PLATFORM),
        ],
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial"), ("billing", "0001_initial")]

    operations = [
        # The price list: any signed-in tenant may read it; only platform staff change it.
        enable_rls(
            "billing_pricingplan",
            [
                Policy("plan_select", "SELECT", using="true"),
                Policy("platform_insert", "INSERT", check=PLATFORM),
                Policy("platform_update", "UPDATE", using=PLATFORM, check=PLATFORM),
            ],
        ),
        platform_only("billing_reseller"),  # reseller deals are internal
        platform_only("billing_paymentevent"),
        tenant_reads_platform_writes("billing_payment"),
        tenant_reads_platform_writes("billing_billingreminder"),
    ]
