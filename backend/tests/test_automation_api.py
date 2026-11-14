"""Automation rules over HTTP: the same promises as the service, through auth, ownership and the real validator."""
from __future__ import annotations

from datetime import datetime, timedelta

from tests.assistant_helpers import TZ, auth_headers, ingest, make_tick
from tests.onboarding_helpers import complete_hybrid_onboarding
from tests.test_automation_service import low_battery

BASE = "/v2/assistant/automations"


async def home(client, email: str) -> dict[str, str]:
    headers = await auth_headers(client, email)
    household_id = await complete_hybrid_onboarding(
        client, headers, appliances=[{"appliance_key": "fridge", "is_critical": True}, {"appliance_key": "heater", "is_critical": False}]
    )
    now = datetime.now(TZ).replace(microsecond=0)
    for i in range(6):  # a little history for the dry run
        await ingest(client, make_tick(now - timedelta(minutes=5 * (6 - i)), household_id=household_id, soc=40 - 5 * i))
    return headers


async def propose(client, headers, rule=None):
    return await client.post(BASE, json={"rule": rule or low_battery()}, headers=headers)


async def test_a_rule_is_proposed_pending_listed_and_approved_with_its_fingerprint(client):
    headers = await home(client, "auto-ok@example.com")

    created = await propose(client, headers)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "pending_approval" and body["summary"].startswith("When battery state of charge")
    assert body["dry_run"]["readings_used"] >= 1 and body["dry_run"]["fired_count"] >= 1

    listed = (await client.get(BASE, headers=headers)).json()
    assert [r["id"] for r in listed] == [body["id"]]

    approved = await client.post(f"{BASE}/{body['id']}/approve", json={"fingerprint": body["fingerprint"]}, headers=headers)
    assert approved.status_code == 200 and approved.json()["status"] == "active"
    assert (await client.get(f"{BASE}/{body['id']}", headers=headers)).json()["status"] == "active"


async def test_approving_with_the_wrong_fingerprint_is_a_409_and_changes_nothing(client):
    headers = await home(client, "auto-fp@example.com")
    body = (await propose(client, headers)).json()

    wrong = await client.post(f"{BASE}/{body['id']}/approve", json={"fingerprint": "f" * 64}, headers=headers)

    assert wrong.status_code == 409 and wrong.json()["error"]["code"] == "automation_fingerprint_mismatch"
    assert (await client.get(f"{BASE}/{body['id']}", headers=headers)).json()["status"] == "pending_approval"


async def test_an_invalid_rule_is_a_422_that_lists_the_problems_found_together(client):
    headers = await home(client, "auto-bad@example.com")
    bad = low_battery()
    bad["trigger"]["metric"] = "battery.mood"
    bad["actions"] = [{"kind": "notify", "channel": "app", "template": "no_such_template"}]

    response = await propose(client, headers, bad)

    assert response.status_code == 422 and response.json()["error"]["code"] == "automation_invalid"
    codes = {p["code"] for p in response.json()["error"]["problems"]}
    assert {"unknown_metric", "unknown_template"} <= codes, "problems of one pass come back together, to be fixed in one go"
    assert all(p["path"] and p["message"] for p in response.json()["error"]["problems"])


async def test_a_rule_naming_a_device_the_home_does_not_have_is_refused(client):
    headers = await home(client, "auto-nodevice@example.com")
    rule = low_battery()
    rule["actions"] = [{"kind": "propose_device_action", "action": "turn_on", "device": "no_such_device"}]

    response = await propose(client, headers, rule)

    assert response.status_code == 422 and response.json()["error"]["problems"]


async def test_another_users_automation_is_a_404_and_cannot_be_changed(client):
    owner = await home(client, "auto-owner@example.com")
    stranger = await home(client, "auto-stranger@example.com")
    body = (await propose(client, owner)).json()

    assert (await client.get(f"{BASE}/{body['id']}", headers=stranger)).status_code == 404
    assert (await client.post(f"{BASE}/{body['id']}/approve", json={"fingerprint": body["fingerprint"]}, headers=stranger)).status_code == 404
    assert (await client.delete(f"{BASE}/{body['id']}", headers=stranger)).status_code == 404
    assert (await client.get(BASE, headers=stranger)).json() == []
    assert (await client.get(f"{BASE}/{body['id']}", headers=owner)).status_code == 200


async def test_pause_resume_reject_and_delete_over_http(client):
    headers = await home(client, "auto-life@example.com")
    first = (await propose(client, headers, low_battery("first"))).json()
    assert (await client.post(f"{BASE}/{first['id']}/reject", headers=headers)).json()["status"] == "rejected"

    second = (await propose(client, headers, low_battery("second"))).json()
    await client.post(f"{BASE}/{second['id']}/approve", json={"fingerprint": second["fingerprint"]}, headers=headers)
    assert (await client.post(f"{BASE}/{second['id']}/pause", headers=headers)).json()["status"] == "paused"
    assert (await client.post(f"{BASE}/{second['id']}/resume", headers=headers)).json()["status"] == "active"
    assert (await client.post(f"{BASE}/{second['id']}/resume", headers=headers)).status_code == 409
    assert (await client.delete(f"{BASE}/{second['id']}", headers=headers)).status_code == 204
    assert (await client.get(f"{BASE}/{second['id']}", headers=headers)).status_code == 404


async def test_it_needs_a_login_and_a_well_formed_fingerprint(client):
    assert (await client.get(BASE)).status_code == 401
    assert (await client.post(BASE, json={"rule": low_battery()})).status_code == 401
    headers = await home(client, "auto-shape@example.com")
    body = (await propose(client, headers)).json()
    short = await client.post(f"{BASE}/{body['id']}/approve", json={"fingerprint": "abc"}, headers=headers)
    assert short.status_code == 400 and short.json()["error"]["code"] == "validation_error"


async def test_a_home_cannot_queue_more_unapproved_rules_than_the_limit(client):
    headers = await home(client, "auto-limit@example.com")
    for i in range(5):
        assert (await propose(client, headers, low_battery(f"r{i}"))).status_code == 201
    over = await propose(client, headers, low_battery("one-too-many"))
    assert over.status_code == 409 and over.json()["error"]["code"] == "automation_limit"
