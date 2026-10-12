"""Phase 9: 'Export my data'."""
import io
import json
import zipfile
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role
from apps.core.db import db_context
from apps.exports import services as export_services
from apps.exports.models import TenantExport

from .helpers import at, client_for, make_forecourt, make_tenant, make_user, run_shift, two_tenants


def request_export(client):
    r = client.post("/api/exports/")
    assert r.status_code == 202, r.content
    return client.get("/api/exports/").json()[0]


def open_zip(client, export_id):
    link = client.post(f"/api/exports/{export_id}/link/").json()["url"]
    res = APIClient().get(link)  # the link works on its own: no sign-in needed, it IS the credential
    assert res.status_code == 200 and res["Content-Type"] == "application/zip"
    return zipfile.ZipFile(io.BytesIO(res.content))


def test_owner_exports_everything_as_json_and_csv_with_no_secrets_and_no_one_elses_data():
    a, b = two_tenants()
    fa, fb = make_forecourt(a["tenant"], name="Alpha Station"), make_forecourt(b["tenant"], name="Beta Station")
    run_shift(a["tenant"], a[Role.ATTENDANT], fa, [0, 0], [100, 0], started_at=at(2026, 10, 1, 6), ended_at=at(2026, 10, 1, 12), cash=[("cash", "USD", 150)])
    owner = client_for(a[Role.OWNER])
    owner.delete(f"/api/pumps/{fa.pump.pk}/")  # a soft-deleted record must still be in their data
    e = request_export(owner)
    assert e["status"] == "ready" and e["size_bytes"] > 0 and e["row_counts"]["operations_transaction"] == 2
    z = open_zip(owner, e["id"])
    names = z.namelist()
    assert "data.json" in names and "README.txt" in names and "csv/stations_station.csv" in names and "csv/operations_shift.csv" in names
    data = json.loads(z.read("data.json"))
    stations = [s["name"] for s in data["tables"]["stations_station"]]
    assert stations == ["Alpha Station"] and "Beta Station" not in z.read("data.json").decode()
    pump = data["tables"]["stations_pump"][0]
    assert pump["deleted_at"] is not None  # included, marked deleted
    users = data["tables"]["accounts_user"]
    assert len(users) == 3 and all("password" not in u and "token_version" not in u for u in users)
    assert "pbkdf2" not in z.read("data.json").decode() and "md5$" not in z.read("data.json").decode()
    assert b"Alpha Station" in z.read("csv/stations_station.csv") and z.read("csv/operations_transaction.csv").count(b"\n") == 3  # header + 2 rows
    del fb


def test_links_are_time_limited_and_tamper_proof(settings):
    a, _ = two_tenants()
    owner = client_for(a[Role.OWNER])
    e = request_export(owner)
    link = owner.post(f"/api/exports/{e['id']}/link/").json()
    assert link["expires_in_seconds"] == settings.EXPORT_LINK_TTL_SECONDS == 86400
    assert APIClient().get(link["url"]).status_code == 200
    assert APIClient().get(link["url"][:-4] + "xxxx").status_code == 404  # tampered
    assert APIClient().get("/api/exports/download/").status_code == 404
    assert APIClient().get("/api/exports/download/?token=garbage").status_code == 404
    settings.EXPORT_LINK_TTL_SECONDS = -1  # expired
    assert APIClient().get(link["url"]).status_code == 404


def test_only_owners_can_export_and_one_business_never_reaches_anothers_files():
    a, b = two_tenants()
    e = request_export(client_for(a[Role.OWNER]))
    for role in (Role.MANAGER, Role.ATTENDANT):
        assert client_for(a[role]).post("/api/exports/").status_code == 403
        assert client_for(a[role]).get("/api/exports/").status_code == 403
    other = client_for(b[Role.OWNER])
    assert other.get("/api/exports/").json() == []
    assert other.post(f"/api/exports/{e['id']}/link/").status_code == 404
    assert APIClient().post(f"/api/exports/{e['id']}/link/").status_code == 401


def test_a_second_request_is_refused_while_one_is_still_being_prepared():
    a, _ = two_tenants()
    with db_context(tenant_id=a["tenant"].id):
        TenantExport.objects.create(tenant_id=a["tenant"].id, status="pending")
    assert client_for(a[Role.OWNER]).post("/api/exports/").status_code == 400


def test_expired_files_are_purged_but_the_record_and_the_business_data_stay():
    from apps.exports import tasks
    from apps.exports.storage import get_storage

    a, _ = two_tenants()
    make_forecourt(a["tenant"])
    owner = client_for(a[Role.OWNER])
    e = request_export(owner)
    with db_context(tenant_id=a["tenant"].id):
        row = TenantExport.objects.get(pk=e["id"])
        key = row.file_key
        TenantExport.objects.filter(pk=row.pk).update(expires_at=timezone.now() - timedelta(days=1))
    assert get_storage().read(key)
    assert tasks.purge_expired_exports.delay().get() == 1
    with pytest.raises(FileNotFoundError):
        get_storage().read(key)
    assert owner.get("/api/exports/").json()[0]["status"] == "expired"
    assert owner.post(f"/api/exports/{e['id']}/link/").status_code == 400
    assert owner.get("/api/stations/").json()["count"] == 1  # live data untouched


def test_monthly_job_exports_every_tenant_even_suspended_ones():
    from apps.exports import tasks

    a, b = two_tenants()
    sus = make_tenant("Quiet Co")
    make_user(sus, Role.OWNER)
    with db_context(platform_admin=True):
        from apps.tenants.models import Tenant

        Tenant.objects.filter(pk=sus.pk).update(status="suspended")
    result = tasks.run_monthly_exports.delay().get()
    assert result["ok"] == 3 and result["failed"] == 0
    for t in (a["tenant"], b["tenant"], sus):
        with db_context(tenant_id=t.id):
            e = TenantExport.objects.get()
            assert e.kind == "monthly" and e.status == "ready" and e.file_key.startswith(f"{t.id}/")


def test_a_failing_export_is_recorded_not_lost(monkeypatch):
    from apps.exports import tasks

    a, _ = two_tenants()
    monkeypatch.setattr(tasks, "build_zip", lambda t: (_ for _ in ()).throw(RuntimeError("disk full")))
    owner = client_for(a[Role.OWNER])
    e = request_export(owner)
    assert e["status"] == "failed" and "disk full" in e["error"]
    assert owner.post(f"/api/exports/{e['id']}/link/").status_code == 400


def test_every_table_holding_tenant_data_is_either_exported_or_consciously_excluded():
    """A new tenant table fails this until a developer decides whether customers get it in their export."""
    with connection.cursor() as cur:
        cur.execute("SELECT DISTINCT table_name FROM information_schema.columns WHERE table_schema='public' AND column_name='tenant_id'")
        with_tenant = {r[0] for r in cur.fetchall()}
    from django.apps import apps

    exported = {apps.get_model(a, m)._meta.db_table for a, m in export_services.EXPORT_MODELS}
    undecided = with_tenant - exported - set(export_services.EXCLUDED)
    assert not undecided, f"Add to EXPORT_MODELS or EXCLUDED (with a reason): {sorted(undecided)}"
    for app_label, name in export_services.EXPORT_MODELS:
        cols = {f.name for f in apps.get_model(app_label, name)._meta.concrete_fields} - export_services.SECRET_FIELDS
        assert "password" not in cols


def test_s3_storage_writes_encrypted_and_issues_short_lived_presigned_links(settings, monkeypatch):
    from botocore.stub import Stubber

    from apps.exports.storage import S3ExportStorage

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATESTTESTTESTTEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    settings.EXPORT_S3_BUCKET = "fuel-exports"
    store = S3ExportStorage()
    with Stubber(store.client) as stub:
        stub.add_response("put_object", {}, {"Bucket": "fuel-exports", "Key": "t/2026-10/x.zip", "Body": b"zip", "ServerSideEncryption": "AES256", "ContentType": "application/zip"})
        store.save("t/2026-10/x.zip", b"zip")
        stub.assert_no_pending_responses()

    class E:
        file_key = "t/2026-10/x.zip"

    url = store.link(E(), 86400)
    assert url.startswith("https://") and "fuel-exports" in url and "X-Amz-Expires=86400" in url and "attachment" in url


def test_support_can_export_one_tenant_from_the_command_line_and_it_is_audited(capsys):
    from django.core.management import call_command
    from django.core.management.base import CommandError

    a, b = two_tenants()
    make_forecourt(a["tenant"], name="Only Alpha")
    make_forecourt(b["tenant"], name="Only Beta")
    call_command("export_tenant", a["tenant"].slug)
    out = capsys.readouterr().out
    assert "/api/exports/download/?token=" in out
    with db_context(tenant_id=a["tenant"].id):
        e = TenantExport.objects.get()
        from apps.exports.storage import get_storage
        z = zipfile.ZipFile(io.BytesIO(get_storage().read(e.file_key)))
        assert "Only Alpha" in z.read("data.json").decode() and "Only Beta" not in z.read("data.json").decode()
        from apps.audit.models import AuditLog
        assert AuditLog.objects.filter(action="export_by_support", via_platform=True).count() == 1
    with pytest.raises(CommandError):
        call_command("export_tenant", "nope")
