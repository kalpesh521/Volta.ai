"""Timescale row mapping + WebSocket auth gate. No live brokers required."""
import uuid

from starlette.testclient import TestClient

from app.modules.energy.pipeline import process_telemetry_body
from app.modules.energy.redis_live import RedisLiveStore
from app.modules.energy.timescale_store import payload_to_record, record_to_params
from app.core.config import settings
from tests.test_ingest_worker import _tick, TOLERANCE
from main import app


def test_record_round_trip_params():
    import json

    decision = process_telemetry_body(json.dumps(_tick()).encode(), TOLERANCE)
    assert decision.record is not None
    params = record_to_params(decision.record)
    assert params["household_id"] == "home_001"
    restored = payload_to_record(params["payload"])
    assert restored.solar_power_kw == decision.record.solar_power_kw
    assert restored.household_id == "home_001"


def test_redis_key_names():
    store = RedisLiveStore()
    assert store.live_key("home_001") == f"{settings.REDIS_KEY_PREFIX}home_001"
    assert store.channel("home_001") == f"{settings.REDIS_KEY_PREFIX}ch:home_001"


def test_websocket_rejects_invalid_token():
    with TestClient(app) as client:
        with client.websocket_connect("/ws/energy?token=nope&household_id=home_001") as ws:
            payload = ws.receive_json()
            assert payload["type"] == "error"
            assert payload["code"] == "invalid_token"


def test_websocket_hello_with_patched_user(monkeypatch):
    class FakeUser:
        id = uuid.uuid4()
        is_active = True

    async def fake_user(_token: str):
        return FakeUser()

    monkeypatch.setattr("app.modules.energy.ws._user_from_token", fake_user)

    async def fake_household(_user, household_id: str):
        return (household_id or "home_001", "")

    monkeypatch.setattr(
        "app.modules.energy.ws._resolve_household_for_socket", fake_household
    )
    with TestClient(app) as client:
        with client.websocket_connect("/ws/energy?token=ok&household_id=home_001") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            assert hello["household_id"] == "home_001"
            ws.close()


def test_websocket_rejects_unowned_household(monkeypatch):
    class FakeUser:
        id = uuid.uuid4()
        is_active = True

    async def fake_user(_token: str):
        return FakeUser()

    async def deny(_user, _household_id: str):
        return None, "household_forbidden"

    monkeypatch.setattr("app.modules.energy.ws._user_from_token", fake_user)
    monkeypatch.setattr(
        "app.modules.energy.ws._resolve_household_for_socket", deny
    )
    with TestClient(app) as client:
        with client.websocket_connect("/ws/energy?token=ok&household_id=home_001") as ws:
            payload = ws.receive_json()
            assert payload["type"] == "error"
            assert payload["code"] == "household_forbidden"
