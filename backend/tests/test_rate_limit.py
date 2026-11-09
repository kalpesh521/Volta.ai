"""Rate limiting: who is counted, and that counters can be shared through Redis.

The Redis tests are opt-in (they need a server):

    TEST_REDIS_URL=redis://localhost:6379/15 pytest tests/test_rate_limit.py
"""
from __future__ import annotations

import os
import uuid

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.responses import JSONResponse

from app.core.rate_limit import build_limiter, rate_limit_key
from app.core.security import create_access_token, generate_opaque_token

REDIS_URL = os.environ.get("TEST_REDIS_URL", "")


def _request(headers: dict[str, str] | None = None, client: str = "10.0.0.7") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "headers": raw, "client": (client, 5000)})


def test_a_valid_access_token_is_limited_per_user():
    user = uuid.uuid4()
    token = create_access_token(user)

    assert rate_limit_key(_request({"Authorization": f"Bearer {token}"})) == f"user:{user}"


def test_two_users_behind_one_address_get_separate_buckets():
    a = rate_limit_key(_request({"Authorization": f"Bearer {create_access_token(uuid.uuid4())}"}))
    b = rate_limit_key(_request({"Authorization": f"Bearer {create_access_token(uuid.uuid4())}"}))

    assert a != b


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer not-a-jwt"},
        {"Authorization": "Basic abc"},
        {"Authorization": f"Bearer {generate_opaque_token()}"},
    ],
)
def test_anything_else_is_limited_by_client_address(headers):
    assert rate_limit_key(_request(headers)) == "ip:10.0.0.7"


def _app(limiter: Limiter, limit: str = "2/minute") -> FastAPI:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)
    app.add_exception_handler(
        RateLimitExceeded, lambda request, exc: JSONResponse({"error": "rate_limited"}, status_code=429)
    )

    @app.get("/ping")
    @limiter.limit(limit)
    async def ping(request: Request) -> dict:
        return {"ok": True}

    return app


async def _statuses(app: FastAPI, count: int, headers: dict[str, str] | None = None) -> list[int]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return [(await client.get("/ping", headers=headers)).status_code for _ in range(count)]


async def test_requests_over_the_limit_get_429():
    app = _app(build_limiter())
    assert await _statuses(app, 3) == [200, 200, 429]


async def test_each_user_has_their_own_allowance():
    app = _app(build_limiter())
    alice = {"Authorization": f"Bearer {create_access_token(uuid.uuid4())}"}
    bob = {"Authorization": f"Bearer {create_access_token(uuid.uuid4())}"}

    assert await _statuses(app, 3, alice) == [200, 200, 429]
    assert await _statuses(app, 2, bob) == [200, 200]


@pytest.mark.skipif(not REDIS_URL, reason="TEST_REDIS_URL is not set")
async def test_replicas_share_counters_through_redis():
    prefix = f"test:{uuid.uuid4().hex}"
    replica_a = _app(build_limiter(REDIS_URL, key_prefix=prefix))
    replica_b = _app(build_limiter(REDIS_URL, key_prefix=prefix))

    assert await _statuses(replica_a, 2) == [200, 200]
    assert await _statuses(replica_b, 1) == [429]  # a different process sees the same count


async def test_an_unreachable_redis_degrades_to_per_process_limits_instead_of_failing():
    app = _app(build_limiter("redis://127.0.0.1:1/0"))

    assert await _statuses(app, 3) == [200, 200, 429]
