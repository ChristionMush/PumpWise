"""The module list (apps/tenants/modules.py): well formed, and reading every live business exactly as it is set up."""
import itertools

import pytest

from apps.tenants import modules
from apps.tenants.models import Tenant

from .test_client_profiles import FUEL_ZONE, LITRIX, PESNAT


def tenant(**settings):
    return Tenant(name="Test", slug="test", **settings)  # never saved: reading modules needs no database


def test_every_module_is_described_and_its_needs_are_on_off_modules_listed_before_it():
    seen = set()
    for m in modules.MODULES:
        assert m.key not in seen and m.label and m.help
        assert all(n in seen and modules.BY_KEY[n].on_off for n in m.needs), m.key
        assert (m.default in dict(m.choices)) if m.choices else m.default is False
        seen.add(m.key)


@pytest.mark.parametrize("mark", modules.MARKS)
def test_each_mark_sets_every_module_to_something_allowed(mark):
    preset = dict(modules.MARKS[mark])
    assert set(preset) == set(modules.BY_KEY) and modules.problems(preset) == []


@pytest.mark.parametrize("live, expected", [
    (FUEL_ZONE, dict(modules.MARKS["mark_1"])),
    (PESNAT, dict(modules.MARKS["mark_2"])),
    (LITRIX, {**modules.MARKS["mark_2"], "attendants_see_dip_litres": False}),
], ids=["fuel_zone_is_mark_1", "pesnat_is_mark_2", "litrix_is_mark_2_without_litres"])
def test_the_live_businesses_read_as_their_mark(live, expected):
    assert modules.of(tenant(**live)).as_dict() == expected


def test_a_new_business_reads_as_the_system_always_was():
    assert modules.of(tenant()).as_dict() == {m.key: m.default for m in modules.MODULES}


@pytest.mark.parametrize("system, grant, pick, expected", [
    ("shared", False, False, "shared"), ("shared", False, True, "shared"),  # picking only counts at assigned pumps
    ("shared_multi", False, True, "shared_multi"), ("assigned", True, False, "assigned"), ("assigned", True, True, "pick"),
    ("shared_multi", True, True, "pick"),  # stations put on assigned pumps one by one: their attendants pick
])
def test_pump_allocation_reads_the_pump_system_and_the_pick_switch(system, grant, pick, expected):
    t = tenant(pump_system=system, multi_attendant_enabled=system != "shared", pump_assignment_enabled=grant, attendants_pick_pumps=pick)
    assert modules.of(t).pump_allocation == expected


@pytest.mark.parametrize("dip, see", itertools.product([False, True], repeat=2))
def test_attendants_see_the_litres_only_when_they_dip_at_the_end_of_a_shift(dip, see):
    mods = modules.of(tenant(dip_at_shift_end=dip, attendants_see_dip_litres=see))
    assert mods.dip_at_shift_end is dip and mods.attendants_see_dip_litres is (dip and see)


def test_problems_with_a_set_of_modules_are_named():
    assert modules.problems({"attendants_see_dip_litres": True}) == ["Attendants see the dip litres needs Attendants dip at the end of a shift."]
    assert modules.problems({"day_close": "21:00"}) == ["Day close: choose one of manager, 07:00, 18:00."]
    assert modules.problems({"expenses": "yes"}) == ["Expenses: on or off."]
    assert modules.problems({"rota": True}) == ["No module called 'rota'."]


def test_modules_are_read_only_and_a_typo_is_an_error():
    mods = modules.of(tenant())
    with pytest.raises(AttributeError):
        mods.expenses = True
    with pytest.raises(AttributeError, match="No module called 'expense'"):
        mods.expense  # noqa: B018


@pytest.mark.parametrize("live, mark", [(FUEL_ZONE, "mark_1"), (PESNAT, "mark_2")], ids=["fuel_zone", "pesnat"])
def test_the_phone_is_told_its_modules_when_it_signs_in(live, mark):
    from apps.accounts.models import Role

    from .helpers import client_for
    from .test_client_profiles import site

    a, *_ = site(live)
    tenant_info = client_for(a[Role.ATTENDANT]).get("/api/auth/me/").json()["tenant"]
    assert tenant_info["modules"] == dict(modules.MARKS[mark])
    assert tenant_info["expenses_enabled"] is live["expenses_enabled"]  # still there for phones on the older version


@pytest.mark.parametrize("stored, handover", [("", ""), ("07:00", "07:00"), ("18:00", "18:00")])
def test_the_handover_the_day_closes_at(stored, handover):
    assert modules.handover(modules.of(tenant(close_day_at_change=stored))) == handover


def test_the_database_filter_finds_exactly_the_businesses_that_close_at_a_handover():
    from apps.core.db import db_context

    from .test_client_profiles import site

    sites = {name: site(live)[0]["tenant"] for name, live in (("fuel_zone", FUEL_ZONE), ("pesnat", PESNAT), ("litrix", LITRIX))}
    with db_context(platform_admin=True):
        found = set(Tenant.objects.filter(modules.closing_at_a_handover()).values_list("pk", flat=True))
        assert found == {t.pk for t in Tenant.objects.all() if modules.handover(modules.of(t))}
    assert found == {sites["pesnat"].pk, sites["litrix"].pk}
