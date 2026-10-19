"""Alerts: managers resolve with a reason, owners review and approve write-offs, nothing is lost from the history,
problems that come back reopen, repeats are spotted, and alerts nobody handles are escalated."""
from datetime import timedelta

from django.utils import timezone

from apps.accounts.models import Role
from apps.core.db import db_context
from apps.insights import alert_actions
from apps.insights.alerts import evaluate_tenant
from apps.insights.models import Alert, AlertEvent, OutboundMessage
from apps.insights.summary import build_daily_summary
from apps.operations.models import TankReading

from .helpers import client_for, dip, make_forecourt, two_tenants


def new_alert(a, fc, kind="loss", key=None, status="open", tank=True):
    with db_context(tenant_id=a["tenant"].id):
        alert = Alert.objects.create(tenant_id=a["tenant"].id, station=fc.station, tank=fc.tank if tank else None, kind=kind, status=status,
                                     message=f"{fc.station.name}: test {kind}", dedupe_key=key or f"{kind}:{timezone.now().timestamp()}",
                                     resolved_at=timezone.now() if status != "open" else None)
        alert_actions.log_event(alert, "opened", note=alert.message)
        return alert


def story(client, alert):
    return [e["kind"] for e in client.get(f"/api/alerts/{alert.pk}/").json()["events"]]


def test_a_managers_resolution_is_explained_and_waits_for_the_owner_to_review_it():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    alert = new_alert(a, fc)
    mgr, owner = client_for(a[Role.MANAGER]), client_for(a[Role.OWNER])
    assert mgr.post(f"/api/alerts/{alert.pk}/resolve/", {"reason": "typing_error"}, format="json").status_code == 400  # a cash reason, on a loss
    done = mgr.post(f"/api/alerts/{alert.pk}/resolve/", {"reason": "meter_typo", "note": "Pump 1 end 1000 should be 100"}, format="json").json()
    assert done["status"] == "resolved" and done["resolution_label"] == "A meter number was mistyped: corrected"
    assert done["resolved_by_role"] == "manager" and done["reviewed_at"] is None
    review = owner.get("/api/alerts/?view=review").json()["results"]
    assert [r["id"] for r in review] == [str(alert.pk)]
    assert owner.get("/api/alerts/summary/").json()["to_review"] == 1
    assert mgr.post(f"/api/alerts/{alert.pk}/accept/").status_code == 403  # only the owner reviews
    owner.post(f"/api/alerts/{alert.pk}/accept/")
    assert owner.get("/api/alerts/?view=review").json()["count"] == 0
    assert story(owner, alert) == ["opened", "resolved", "reviewed"]


def test_write_offs_need_the_owners_approval():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    alert = new_alert(a, fc)
    mgr, owner = client_for(a[Role.MANAGER]), client_for(a[Role.OWNER])
    assert mgr.post(f"/api/alerts/{alert.pk}/resolve/", {"reason": "real_loss"}, format="json").status_code == 400  # a note is required
    waiting = mgr.post(f"/api/alerts/{alert.pk}/resolve/", {"reason": "real_loss", "note": "Leak at the tank valve, 400 L"}, format="json").json()
    assert waiting["status"] == "awaiting_approval"
    assert [r["id"] for r in mgr.get("/api/alerts/").json()["results"]] == [str(alert.pk)]  # still shown to the manager, waiting
    assert mgr.post(f"/api/alerts/{alert.pk}/approve/").status_code == 403
    approved = owner.post(f"/api/alerts/{alert.pk}/approve/", {"note": "Valve to be replaced"}, format="json").json()
    assert approved["status"] == "resolved" and approved["reviewed_by_name"] == a[Role.OWNER].full_name
    assert story(owner, alert) == ["opened", "submitted_for_approval", "approved"]


def test_the_owner_can_send_a_resolution_back_with_a_note():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    alert = new_alert(a, fc, kind="cash", tank=False)
    mgr, owner = client_for(a[Role.MANAGER]), client_for(a[Role.OWNER])
    mgr.post(f"/api/alerts/{alert.pk}/resolve/", {"reason": "handed_in_late"}, format="json")
    assert owner.post(f"/api/alerts/{alert.pk}/send-back/", {}, format="json").status_code == 400  # say what still needs doing
    back = owner.post(f"/api/alerts/{alert.pk}/send-back/", {"note": "Show me the late cash slip"}, format="json").json()
    assert back["status"] == "open" and back["resolution_reason"] == "handed_in_late"
    assert [r["id"] for r in mgr.get("/api/alerts/").json()["results"]] == [str(alert.pk)]
    events = owner.get(f"/api/alerts/{alert.pk}/").json()["events"]
    assert events[-1]["kind"] == "sent_back" and events[-1]["note"] == "Show me the late cash slip"
    # the owner resolving it themselves needs no review
    owner.post(f"/api/alerts/{alert.pk}/resolve/", {"reason": "other", "note": "Slip seen"}, format="json")
    assert owner.get("/api/alerts/summary/").json()["to_review"] == 0


def test_history_lists_every_alert_for_the_owner_and_only_their_station_for_a_manager():
    a, _ = two_tenants()
    north, south = make_forecourt(a["tenant"], name="North"), make_forecourt(a["tenant"], name="South", code="DIESEL")
    new_alert(a, north)
    new_alert(a, south, status="auto_resolved")
    owner, mgr = client_for(a[Role.OWNER]), client_for(a[Role.MANAGER])
    assert owner.get("/api/alerts/?view=history").json()["count"] == 2
    assert owner.get("/api/alerts/?view=history&status=auto_resolved").json()["count"] == 1
    owner.put(f"/api/users/{a[Role.MANAGER].id}/stations/", {"station_ids": [str(north.station.pk)]}, format="json")
    assert [r["station_name"] for r in mgr.get("/api/alerts/?view=history").json()["results"]] == ["North"]
    assert client_for(a[Role.ATTENDANT]).get("/api/alerts/?view=history").status_code == 403


def test_a_problem_that_comes_back_within_a_day_reopens_the_same_alert():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    tid = a["tenant"].id
    with db_context(tenant_id=tid):
        evaluate_tenant(tid)  # never dipped: missed reading
        first = Alert.objects.get(kind="missed_reading", tank=fc.tank)
    dip(a["tenant"], a[Role.MANAGER], fc, 5000, timezone.now())
    with db_context(tenant_id=tid):
        evaluate_tenant(tid)
        assert Alert.objects.get(pk=first.pk).status == "auto_resolved"
        TankReading.objects.filter(tank=fc.tank).update(reading_at=timezone.now() - timedelta(hours=60))
        assert evaluate_tenant(tid)["reopened"] == 1
        again = Alert.objects.get(pk=first.pk)
        assert again.status == "open" and again.reopened_count == 1
        assert Alert.objects.filter(kind="missed_reading", tank=fc.tank).count() == 1
        assert list(AlertEvent.objects.filter(alert=first).values_list("kind", flat=True)) == ["opened", "auto_resolved", "reopened"]


def test_alerts_nobody_handles_remind_the_manager_after_12_hours_and_reach_the_owner_after_24():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    alert = new_alert(a, fc)
    tid = a["tenant"].id
    with db_context(tenant_id=tid):
        AlertEvent.objects.filter(alert=alert).update(created_at=timezone.now() - timedelta(hours=13))
        assert alert_actions.escalate(tid) == {"reminded": 1, "escalated": 0}
        assert alert_actions.escalate(tid) == {"reminded": 0, "escalated": 0}  # once
        assert OutboundMessage.objects.filter(kind="alert_reminder").count() == 1
        AlertEvent.objects.filter(alert=alert, kind="opened").update(created_at=timezone.now() - timedelta(hours=25))
        assert alert_actions.escalate(tid)["escalated"] == 1
        assert OutboundMessage.objects.filter(kind="alert_escalated").count() == 1
        assert {"reminded", "escalated"} <= set(AlertEvent.objects.filter(alert=alert).values_list("kind", flat=True))


def test_repeats_point_at_the_cause_and_the_daily_message_says_what_managers_did():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    new_alert(a, fc, status="resolved")
    new_alert(a, fc, status="resolved")
    third = new_alert(a, fc)
    tid = a["tenant"].id
    with db_context(tenant_id=tid):
        alert_actions.apply_patterns()
        assert "3 fuel losses on this tank in 7 days" in Alert.objects.get(pk=third.pk).pattern
    client_for(a[Role.MANAGER]).post(f"/api/alerts/{third.pk}/resolve/", {"reason": "dip_misread", "note": "Re-dipped at 15:00"}, format="json")
    with db_context(tenant_id=tid):
        text = build_daily_summary(tid)
    assert "Managers resolved 1 alert: 1 Dip was misread: re-dipped" in text


def test_a_standing_condition_a_person_resolved_stays_quiet_for_a_day_then_comes_back_if_still_there():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    tid = a["tenant"].id
    with db_context(tenant_id=tid):
        evaluate_tenant(tid)  # never dipped
        alert = Alert.objects.get(kind="missed_reading", tank=fc.tank)
    client_for(a[Role.MANAGER]).post(f"/api/alerts/{alert.pk}/resolve/", {"reason": "other", "note": "Dip stick broken, new one Friday"}, format="json")
    with db_context(tenant_id=tid):
        evaluate_tenant(tid)  # still no dip, but the manager answered less than a day ago
        assert Alert.objects.get(pk=alert.pk).status == "resolved"
        assert Alert.objects.filter(kind="missed_reading", tank=fc.tank).count() == 1
        Alert.objects.filter(pk=alert.pk).update(resolved_at=timezone.now() - timedelta(hours=25))
        evaluate_tenant(tid)  # a day later and still not dipped: the same alert opens again
        again = Alert.objects.get(pk=alert.pk)
        assert again.status == "open" and again.reopened_count == 1
