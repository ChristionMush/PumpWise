"""Module "start_before_approval": an attendant starts their next shift while their last end still waits for the manager.
On in Mark 2 (PESNAT). Off (the default, Mark 1): no new shift until it is approved (test_shift_approval)."""
from apps.accounts.models import Role

from .helpers import client_for, make_forecourt, platform_client, two_tenants
from .test_shift_approval import end, ended, stage, start


def module_on(a):
    res = platform_client().patch(f"/api/platform/tenants/{a['tenant'].id}/modules/", {"modules": {"start_before_approval": True}}, format="json")
    assert res.status_code == 200, res.content


def test_a_new_shift_starts_while_the_last_end_waits_and_the_manager_approves_it_later():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    module_on(a)
    att, mgr = client_for(a[Role.ATTENDANT]), client_for(a[Role.MANAGER])
    first, end_id = ended(a, fc)
    held = stage(att)
    assert held["stage"] == "held" and held["status"] == "pending" and held["can_start"] is True  # the phone offers "Start my next shift"

    res = start(att, fc, (1100, 5040))
    assert res.status_code == 201, res.content
    second = res.json()["id"]
    on = stage(att)
    assert on["stage"] == "on_shift" and on["shift"]["id"] == second  # any phone shows the new shift, not the waiting end

    assert mgr.get("/api/approvals/summary/").json()["ends"] == 1  # still in front of the manager
    assert mgr.post(f"/api/shift-ends/{end_id}/approve/").json()["status"] == "approved"
    assert stage(att)["stage"] == "on_shift"
    assert end(att, fc, second, readings=(1200, 5080)).status_code == 200
    assert first != second


def test_an_end_sent_back_to_fix_still_holds_them():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    module_on(a)
    att, mgr = client_for(a[Role.ATTENDANT]), client_for(a[Role.MANAGER])
    _, end_id = ended(a, fc, cash="150")
    assert mgr.post(f"/api/shift-ends/{end_id}/send-back/", {"note": "Recount the cash"}, format="json").status_code == 200
    held = stage(att)
    assert held["status"] == "sent_back" and "can_start" not in held
    refused = start(att, fc, (1100, 5040))
    assert refused.status_code == 400 and "sent back to fix" in str(refused.json())


def test_ended_by_mistake_is_refused_once_they_are_on_a_new_shift():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    module_on(a)
    att, owner = client_for(a[Role.ATTENDANT]), client_for(a[Role.OWNER])
    _, end_id = ended(a, fc)
    assert start(att, fc, (1100, 5040)).status_code == 201
    req = att.post(f"/api/shift-ends/{end_id}/undo/", {"kind": "reopen", "reason": "I pressed End at lunch"}, format="json").json()
    allowed = owner.post(f"/api/undo-requests/{req['id']}/allow/")
    assert allowed.status_code == 400  # never on two shifts at once: the manager corrects the numbers instead


def test_off_by_default_so_fuel_zone_is_unchanged():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    att = client_for(a[Role.ATTENDANT])
    ended(a, fc)
    assert "can_start" not in stage(att)
    assert start(att, fc, (1100, 5040)).status_code == 400
