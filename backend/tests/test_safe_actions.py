"""Safe virtual actions: proposal, approval, audit, alerts, and recommendations."""
from datetime import datetime

from app.core.config import settings
from app.modules.assistant.actions.deps import get_command_publisher
from app.modules.assistant.actions.execution import command_payload, record_virtual_result
from app.modules.assistant.actions.publisher import RecordingPublisher
from app.modules.assistant.domain.recommendations import (
    DeviceOption,
    HourPoint,
    find_best_device_schedule,
    identify_solar_surplus_window,
)
from main import app
from tests.assistant_helpers import TZ, auth_headers, ingest, make_tick
from tests.onboarding_helpers import complete_hybrid_onboarding

HEATER = {
    "device_id": "dev_water_heater",
    "device_name": "Water heater",
    "device_type": "water_heater",
    "rated_power_kw": 2.0,
    "current_state": "off",
    "current_power_kw": 0.0,
    "energy_interval_kwh": 0.0,
    "critical": False,
    "controllable": True,
}


def test_surplus_window_and_device_choice_are_calculated():
    window = identify_solar_surplus_window(
        [
            HourPoint("10:00", 1.2, 0.4, 0.0),
            HourPoint("11:00", 1.4, 0.5, 0.0),
            HourPoint("12:00", 0.2, 0.8, 0.6),
        ]
    )
    assert window["start_hour"] == "10:00"
    assert window["hours"] == 2
    choice = find_best_device_schedule(
        [DeviceOption("water_heater", 1.5, True, False, "off"), DeviceOption("refrigerator", 0.15, False, True, "on")],
        surplus_kw=2.4,
        soc_percent=70,
        reserve_percent=30,
    )
    assert choice["device"] == "water_heater"
    assert choice["duration_minutes"] == 30


async def test_water_heater_is_proposed_approved_and_audited(client, db_session):
    publisher = RecordingPublisher()
    app.dependency_overrides[get_command_publisher] = lambda: publisher
    previous_enabled = settings.DEVICE_CONTROL_ENABLED
    previous_kill = settings.DEVICE_KILL_SWITCH
    settings.DEVICE_CONTROL_ENABLED = True
    settings.DEVICE_KILL_SWITCH = False
    try:
        headers = await auth_headers(client, "safe-action@example.com")
        household_id = await complete_hybrid_onboarding(client, headers)
        now = datetime.now(TZ).replace(microsecond=0)
        await ingest(
            client,
            make_tick(now, household_id=household_id, soc=70, solar_kw=3.2, load_kw=0.8, devices=[HEATER]),
        )

        proposed = await client.post(
            "/assistant/actions/propose",
            json={"question": "Run the water heater now."},
            headers=headers,
        )
        assert proposed.status_code == 200, proposed.text
        body = proposed.json()
        assert body["status"] == "awaiting_confirmation"
        assert body["device"] == "water_heater"
        assert body["command"] == "on"
        assert body["checks"]["telemetry_freshness"] == "pass"
        assert body["checks"]["grid_outage"] == "pass"
        assert publisher.commands == []

        confirmed = await client.post(
            f"/assistant/actions/{body['proposal_id']}/confirm",
            json={"confirmed": True},
            headers=headers,
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["status"] == "queued"
        assert "has not changed" in confirmed.json()["message"]
        assert len(publisher.commands) == 1

        result = await record_virtual_result(db_session, command_payload(publisher.commands[0]))
        assert result["applied"] is True
        assert result["status"] == "virtual_applied"

        status = await client.get(f"/assistant/actions/{body['proposal_id']}", headers=headers)
        assert status.status_code == 200, status.text
        recorded = status.json()
        assert recorded["status"] == "applied"
        events = [item["event"] for item in recorded["audit"]]
        assert events == ["proposed", "confirmed", "queued", "applied"]

        again = await client.post(
            "/assistant/actions/propose",
            json={"question": "Run the water heater now."},
            headers=headers,
        )
        assert again.json()["status"] == "rejected"
        assert "duplicate_action" in again.json()["reasons"]

        waiting = await client.get("/assistant/notifications", headers=headers)
        waiting_row = next(item for item in waiting.json() if item["kind"] == "action_waiting_confirmation")
        assert waiting_row["priority"] == "info"
        assert waiting_row["channels"] == ["in_app"]

        advice = await client.get("/assistant/me/recommendations", headers=headers)
        assert advice.status_code == 200, advice.text
        assert advice.json()["surplus_kw"] == 2.4
        assert advice.json()["best_device"]["device"] == "water_heater"
    finally:
        settings.DEVICE_CONTROL_ENABLED = previous_enabled
        settings.DEVICE_KILL_SWITCH = previous_kill
        app.dependency_overrides.pop(get_command_publisher, None)


async def test_reject_cancel_suggest_and_alert(client):
    publisher = RecordingPublisher()
    app.dependency_overrides[get_command_publisher] = lambda: publisher
    previous_enabled = settings.DEVICE_CONTROL_ENABLED
    previous_kill = settings.DEVICE_KILL_SWITCH
    settings.DEVICE_CONTROL_ENABLED = True
    settings.DEVICE_KILL_SWITCH = False
    try:
        headers = await auth_headers(client, "safe-rules@example.com")
        household_id = await complete_hybrid_onboarding(client, headers)
        now = datetime.now(TZ).replace(microsecond=0)
        await ingest(
            client,
            make_tick(now, household_id=household_id, soc=70, import_kw=1.6, devices=[HEATER]),
        )

        proposed = await client.post(
            "/assistant/actions/propose",
            json={"question": "Turn on the water heater"},
            headers=headers,
        )
        proposal_id = proposed.json()["proposal_id"]
        rejected = await client.post(f"/assistant/actions/{proposal_id}/reject", headers=headers)
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["status"] == "rejected"
        assert publisher.commands == []

        second = await client.post(
            "/assistant/actions/propose",
            json={"question": "Turn on the water heater"},
            headers=headers,
        )
        cancelled = await client.post(
            f"/assistant/actions/{second.json()['proposal_id']}/cancel",
            headers=headers,
        )
        assert cancelled.json()["status"] == "cancelled"

        created = await client.post(
            "/assistant/automations",
            json={
                "name": "Surplus geyser",
                "device": "water_heater",
                "command": "on",
                "surplus_kw_min": 1.0,
                "soc_percent_min": 60,
                "mode": "suggest_only",
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        evaluated = await client.post("/assistant/automations/evaluate", headers=headers)
        assert evaluated.status_code == 200, evaluated.text
        assert evaluated.json()[0]["matched"] is True
        assert evaluated.json()[0]["proposal_id"] is None

        notes = await client.get("/assistant/notifications", headers=headers)
        assert any(item["kind"] == "automation_suggestion" for item in notes.json())

        scanned = await client.post("/assistant/notifications/scan", headers=headers)
        assert scanned.status_code == 200, scanned.text
        imported = next(item for item in scanned.json() if item["kind"] == "high_grid_import")
        assert imported["priority"] == "warning"
        assert imported["channels"] == ["in_app"]
        assert imported["email_status"] == "skipped"
    finally:
        settings.DEVICE_CONTROL_ENABLED = previous_enabled
        settings.DEVICE_KILL_SWITCH = previous_kill
        app.dependency_overrides.pop(get_command_publisher, None)
