"""Marks (apps/tenants/marks.py): named sets of modules a business starts from. Editing or duplicating a Mark never
changes a business; only the platform sees or changes them."""
import importlib

import pytest

from apps.accounts.models import Role
from apps.audit.models import AuditLog
from apps.core.db import db_context
from apps.tenants import marks, modules
from apps.tenants.models import Mark, Tenant

from .helpers import client_for, platform_client
from .test_client_profiles import FUEL_ZONE, LITRIX, PESNAT, site

seed = importlib.import_module("apps.tenants.migrations.0019_mark")


def starting_marks():
    with db_context(platform_admin=True):
        marks.ensure_starting_marks()
        return {m.name: m for m in Mark.objects.all()}


def on_mark(profile, mark):
    a, *_ = site(profile)
    assert platform_client().patch(f"/api/platform/tenants/{a['tenant'].id}/mark/", {"mark": str(mark.pk)}, format="json").status_code == 200
    return a


def test_the_migration_seeds_the_marks_as_the_code_describes_them_and_reads_modules_the_same_way():
    assert seed.MARK_1 == dict(modules.MARKS["mark_1"]) and seed.MARK_2 == dict(modules.MARKS["mark_2"])
    for live in (FUEL_ZONE, PESNAT, LITRIX):
        t = Tenant(name="T", slug="t", **live)
        assert seed._modules(t) == modules.of(t).as_dict()


@pytest.mark.parametrize("live, expected", [(FUEL_ZONE, "Mark 1"), (PESNAT, "Mark 2"), (LITRIX, "Mark 2")], ids=["fuel_zone", "pesnat", "litrix"])
def test_the_migration_links_each_live_business_to_its_closest_mark(live, expected):
    mods = seed._modules(Tenant(name="T", slug="t", **live))
    closest = min(seed.MARKS, key=lambda m: sum(mods[k] != v for k, v in m[2].items()))
    assert closest[0] == expected


def test_the_platform_lists_every_module_to_tick():
    body = platform_client().get("/api/platform/modules/").json()["modules"]
    assert [m["key"] for m in body] == [m.key for m in modules.MODULES]
    litres = next(m for m in body if m["key"] == "attendants_see_dip_litres")
    assert litres["kind"] == "on_off" and litres["needs"] == ["dip_at_shift_end"]
    day = next(m for m in body if m["key"] == "day_close")
    assert day["kind"] == "choice" and [c["value"] for c in day["choices"]] == ["manager", "07:00", "18:00"]


def test_the_platform_lists_the_marks_with_the_businesses_on_them():
    m = starting_marks()
    fz, pes = on_mark(FUEL_ZONE, m["Mark 1"]), on_mark(PESNAT, m["Mark 2"])
    listed = {x["name"]: x for x in platform_client().get("/api/platform/marks/").json()["marks"]}
    assert listed["Mark 1"]["modules"] == dict(modules.MARKS["mark_1"]) and listed["Mark 2"]["modules"] == dict(modules.MARKS["mark_2"])
    assert [t["id"] for t in listed["Mark 1"]["tenants"]] == [str(fz["tenant"].id)]
    assert [t["id"] for t in listed["Mark 2"]["tenants"]] == [str(pes["tenant"].id)]


def test_a_business_shows_its_mark_its_modules_and_where_they_differ():
    m = starting_marks()
    pes, lit = on_mark(PESNAT, m["Mark 2"]), on_mark(LITRIX, m["Mark 2"])
    p = platform_client()
    body = p.get(f"/api/platform/tenants/{pes['tenant'].id}/").json()
    assert body["mark"]["name"] == "Mark 2" and body["modules"] == dict(modules.MARKS["mark_2"]) and body["mark_differences"] == {}
    body = p.get(f"/api/platform/tenants/{lit['tenant'].id}/").json()
    assert body["mark_differences"] == {"attendants_see_dip_litres": [True, False]}  # Litrix: Mark 2 without the litres


def test_duplicating_a_mark_and_changing_the_copy_leaves_the_original_and_its_businesses_as_they_were():
    m = starting_marks()
    pes = on_mark(PESNAT, m["Mark 2"])
    p = platform_client()
    made = p.post("/api/platform/marks/", {"duplicate_of": str(m["Mark 2"].pk), "name": "Mark 3"}, format="json")
    assert made.status_code == 201, made.content
    mark3 = made.json()
    assert mark3["modules"] == dict(modules.MARKS["mark_2"]) and mark3["based_on"]["name"] == "Mark 2"
    changed = p.patch(f"/api/platform/marks/{mark3['id']}/", {"modules": {"day_close": "07:00", "expenses": False}}, format="json")
    assert changed.status_code == 200 and changed.json()["modules"]["day_close"] == "07:00"
    assert p.get(f"/api/platform/marks/{m['Mark 2'].pk}/").json()["modules"] == dict(modules.MARKS["mark_2"])  # the original
    assert p.get(f"/api/platform/tenants/{pes['tenant'].id}/").json()["modules"] == dict(modules.MARKS["mark_2"])  # its business


def test_editing_a_mark_never_changes_a_business_on_it():
    m = starting_marks()
    fz = on_mark(FUEL_ZONE, m["Mark 1"])
    p = platform_client()
    assert p.patch(f"/api/platform/marks/{m['Mark 1'].pk}/", {"modules": {"expenses": True}}, format="json").status_code == 200
    with db_context(platform_admin=True):
        assert Tenant.objects.get(pk=fz["tenant"].id).expenses_enabled is False  # Fuel Zone exactly as it was
    body = p.get(f"/api/platform/tenants/{fz['tenant'].id}/").json()
    assert body["modules"]["expenses"] is False and body["mark_differences"] == {"expenses": [True, False]}
    with db_context(platform_admin=True):
        assert AuditLog.objects.filter(action="mark_updated").get().changes["modules"][1]["expenses"] is True


@pytest.mark.parametrize("body, field", [
    ({"name": "Mark 1", "modules": dict(modules.MARKS["mark_1"])}, "name"),  # taken
    ({"name": " ", "modules": dict(modules.MARKS["mark_1"])}, "name"),
    ({"name": "Mark 9", "modules": {"expenses": True}}, "modules"),  # every module needs a value
    ({"name": "Mark 9", "modules": {**modules.MARKS["mark_1"], "attendants_see_dip_litres": True}}, "modules"),  # needs the dip
    ({"name": "Mark 9", "modules": {**modules.MARKS["mark_1"], "day_close": "21:00"}}, "modules"),
    ({"name": "Mark 9", "modules": {**modules.MARKS["mark_1"], "rota": True}}, "modules"),
])
def test_a_mark_that_does_not_make_sense_is_refused(body, field):
    starting_marks()
    res = platform_client().post("/api/platform/marks/", body, format="json")
    assert res.status_code == 400 and field in res.json(), res.content


def test_only_the_platform_sees_or_changes_marks():
    m = starting_marks()
    a = on_mark(PESNAT, m["Mark 2"])
    for role in (Role.OWNER, Role.MANAGER, Role.ATTENDANT):
        c = client_for(a[role])
        assert c.get("/api/platform/marks/").status_code == 403
        assert c.patch(f"/api/platform/marks/{m['Mark 2'].pk}/", {"name": "Mine"}, format="json").status_code == 403
        assert c.patch(f"/api/platform/tenants/{a['tenant'].id}/mark/", {"mark": None}, format="json").status_code == 403
    with db_context(tenant_id=a["tenant"].id):
        assert not Mark.objects.exists()  # the database hides the marks from a business too


def test_linking_a_business_to_another_mark_changes_only_the_label():
    m = starting_marks()
    a = on_mark(PESNAT, m["Mark 2"])
    res = platform_client().patch(f"/api/platform/tenants/{a['tenant'].id}/mark/", {"mark": str(m["Mark 1"].pk)}, format="json")
    assert res.status_code == 200 and res.json()["mark"]["name"] == "Mark 1"
    assert res.json()["modules"] == dict(modules.MARKS["mark_2"])  # still works the PESNAT way
    with db_context(tenant_id=a["tenant"].id):  # the business's own audit log shows it
        assert AuditLog.objects.filter(action="tenant_mark_set").first().changes == {"mark": ["Mark 2", "Mark 1"]}  # newest first


def new_business(**body):
    from .helpers import NEW_TENANT

    return {**NEW_TENANT, "owner_phone": None, "owner_email": f"{body['business_name'].split()[0].lower()}@example.com", **body}


def test_a_new_business_starts_from_its_mark():
    m = starting_marks()
    res = platform_client().post("/api/platform/tenants/", new_business(business_name="Domboshava Fuel", mark=str(m["Mark 2"].pk)), format="json")
    assert res.status_code == 201, res.content
    body = res.json()
    assert body["mark"]["name"] == "Mark 2" and body["modules"] == dict(modules.MARKS["mark_2"]) and body["mark_differences"] == {}
    assert {(s["attendants_mode"], s["assignment_mode"]) for s in body["stations"]} == {("multiple", "assigned")}


def test_a_new_business_takes_its_marks_modules_with_one_borrowed_from_the_other_mark():
    m = starting_marks()
    ticks = {**modules.MARKS["mark_1"], "expenses": True}  # Mark 1, plus Mark 2's expenses
    res = platform_client().post("/api/platform/tenants/", new_business(business_name="Kwekwe Fuel", mark=str(m["Mark 1"].pk), modules=ticks), format="json")
    assert res.status_code == 201, res.content
    body = res.json()
    assert body["modules"] == ticks and body["mark_differences"] == {"expenses": [False, True]}
    assert body["pump_system"] == "assigned" and body["features"]["expenses_enabled"] is True


def test_a_new_business_with_modules_that_do_not_fit_is_refused_and_nothing_is_made():
    m = starting_marks()
    ticks = {**modules.MARKS["mark_1"], "attendants_see_dip_litres": True}  # litres without the dip
    res = platform_client().post("/api/platform/tenants/", new_business(business_name="Gweru Fuel", mark=str(m["Mark 1"].pk), modules=ticks), format="json")
    assert res.status_code == 400 and "modules" in res.json()
    with db_context(platform_admin=True):
        assert not Tenant.objects.filter(name="Gweru Fuel").exists()


def test_the_platform_changes_a_businesss_modules_each_through_its_own_setter():
    m = starting_marks()
    a = on_mark(FUEL_ZONE, m["Mark 1"])
    p = platform_client()
    res = p.patch(f"/api/platform/tenants/{a['tenant'].id}/modules/", {"modules": {"expenses": True, "day_close": "18:00"}}, format="json")
    assert res.status_code == 200, res.content
    assert res.json()["modules"]["expenses"] is True and res.json()["modules"]["day_close"] == "18:00"
    assert res.json()["mark_differences"] == {"expenses": [False, True], "day_close": ["manager", "18:00"]}
    with db_context(tenant_id=a["tenant"].id):
        assert set(AuditLog.objects.values_list("action", flat=True)) >= {"tenant_expenses_set", "tenant_shift_options_set"}
    refused = p.patch(f"/api/platform/tenants/{a['tenant'].id}/modules/", {"modules": {"attendants_see_dip_litres": True}}, format="json")
    assert refused.status_code == 400 and "modules" in refused.json()


def test_the_pump_allocation_waits_for_open_shifts_but_other_modules_do_not():
    from .test_client_profiles import DAY_START
    from .test_multi_attendant import readings

    m = starting_marks()
    a, fc, att_a, *_ = site(FUEL_ZONE)
    platform_client().patch(f"/api/platform/tenants/{a['tenant'].id}/mark/", {"mark": str(m["Mark 1"].pk)}, format="json")
    assert client_for(att_a).post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": DAY_START,
                                                         "readings": readings(fc.nozzles, [0, 0])}, format="json").status_code == 201
    p = platform_client()
    busy = p.patch(f"/api/platform/tenants/{a['tenant'].id}/modules/", {"modules": {"pump_allocation": "shared"}}, format="json")
    assert busy.status_code == 400 and "A shift is open" in str(busy.json())
    # pick keeps the stations on assigned pumps, so it is not held up; nor is a module left as it is
    ok = p.patch(f"/api/platform/tenants/{a['tenant'].id}/modules/", {"modules": {"pump_allocation": "pick", "expenses": True}}, format="json")
    assert ok.status_code == 200 and ok.json()["modules"]["pump_allocation"] == "pick"
