"""Alert routing, in-process fan-out, and the notification WebSocket. No broker required."""
import uuid

from starlette.testclient import TestClient

from app.modules.assistant.domain.alerts import alerts_from_home, channels_for, priority_for
from app.modules.assistant.notifications.bus import local_hub, publish
from app.modules.assistant.notifications.email import email_configured, resolved_provider
from app.modules.assistant.notifications.redis_client import notification_list_key
from app.core.config import settings
from main import app


def test_alert_priority_and_channels():
    drafts = alerts_from_home(
        soc_percent=10,
        reserve_percent=20,
        grid_status="outage",
        import_kw=2.2,
        solar_kw=0.0,
        radiation_wm2=640,
        data_quality="disconnected",
        fault_code="E1",
        daily_ready=True,
        insight="Run the water heater now.",
    )
    by_kind = {item.kind: item.priority for item in drafts}
    assert by_kind["low_battery"] == "critical"
    assert by_kind["grid_outage"] == "critical"
    assert by_kind["inverter_fault"] == "critical"
    assert by_kind["high_grid_import"] == "warning"
    assert by_kind["solar_generation_low"] == "warning"
    assert by_kind["sensor_offline"] == "warning"
    assert by_kind["daily_energy_summary_ready"] == "info"
    assert by_kind["recommendation_ready"] == "info"
    assert "grid_restored" not in by_kind
    assert channels_for("critical") == ("in_app", "email")
    assert channels_for("warning") == ("in_app",)
    assert channels_for("info") == ("in_app",)
    assert priority_for("device_command_failed") == "warning"


def test_grid_restored_is_info_after_an_outage():
    drafts = alerts_from_home(
        soc_percent=80,
        reserve_percent=20,
        grid_status="available",
        import_kw=0.2,
        solar_kw=1.5,
        radiation_wm2=100,
        data_quality="good",
        fault_code=None,
        previous_outage=True,
    )
    restored = next(item for item in drafts if item.kind == "grid_restored")
    assert restored.priority == "info"
    assert channels_for(restored.priority) == ("in_app",)


def test_email_stays_off_without_a_provider(monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_PROVIDER", "")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "")
    monkeypatch.setattr(settings, "SENDGRID_API_KEY", "")
    assert resolved_provider() == ""
    assert email_configured() is False


def test_notification_cache_key_is_per_user_and_home():
    user_id = "ff4a0e98-521b-4589-9a97-c45dc9be198e"
    assert (
        notification_list_key(user_id, "home_001")
        == f"{settings.NOTIFICATION_KEY_PREFIX}list:{user_id}:home_001"
    )


async def test_publish_falls_back_to_the_local_hub(monkeypatch):
    async def no_redis():
        return None

    monkeypatch.setattr("app.modules.assistant.notifications.bus.notification_redis", no_redis)
    queue = local_hub.subscribe("home_notify_test")
    try:
        await publish("home_notify_test", {"type": "notification", "notification": {"id": "1"}})
        assert queue.get_nowait()["notification"]["id"] == "1"
    finally:
        local_hub.unsubscribe("home_notify_test", queue)


def test_notification_socket_rejects_invalid_token():
    with TestClient(app) as client:
        with client.websocket_connect("/ws/notifications?token=nope&household_id=home_001") as ws:
            payload = ws.receive_json()
            assert payload["type"] == "error"
            assert payload["code"] == "invalid_token"


def test_notification_socket_hello(monkeypatch):
    class FakeUser:
        id = uuid.uuid4()
        is_active = True

    async def fake_user(_token: str):
        return FakeUser()

    async def fake_household(_user, household_id: str):
        return (household_id or "home_001", "")

    async def fake_unread(_user_id, _household_id):
        return 1, [{"id": "n1", "kind": "grid_outage", "priority": "critical", "title": "Grid outage", "body": "Down"}]

    monkeypatch.setattr("app.modules.assistant.notifications.ws._user_from_token", fake_user)
    monkeypatch.setattr("app.modules.assistant.notifications.ws._resolve_household_for_socket", fake_household)
    monkeypatch.setattr("app.modules.assistant.notifications.ws.load_unread", fake_unread)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/notifications?token=ok&household_id=home_001") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            assert hello["household_id"] == "home_001"
            assert hello["unread_count"] == 1
            assert hello["notifications"][0]["kind"] == "grid_outage"
            ws.close()
