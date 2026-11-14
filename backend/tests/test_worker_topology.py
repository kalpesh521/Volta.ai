"""Several uvicorn workers per pod (Phase 5.2): the startup guard, the entrypoint and pod-wide metrics."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.startup_guards import enforce_worker_topology, evaluate_worker_topology
from app.serve import MULTIPROC_ENV, build_uvicorn_kwargs, prepare_multiprocess_metrics

SAFE = dict(
    web_concurrency=4,
    platform_store="redis",
    checkpoint_backend="postgres",
    rate_limit_storage_uri="redis://redis:6379/1",
    environment="production",
)


def _evaluate(**overrides):
    return evaluate_worker_topology(**{**SAFE, **overrides})


# --- the guard -----------------------------------------------------------------------------


def test_a_fully_shared_configuration_is_clean_with_many_workers():
    report = _evaluate()
    assert report.ok and report.warnings == []


def test_one_worker_may_keep_everything_in_memory_even_in_production():
    report = _evaluate(
        web_concurrency=1, platform_store="memory", checkpoint_backend="memory", rate_limit_storage_uri=""
    )
    assert report.ok and report.warnings == []


def test_production_refuses_memory_platform_store_with_more_than_one_worker():
    report = _evaluate(platform_store="memory")
    assert not report.ok
    assert "AI_PLATFORM_STORE=memory" in report.errors[0] and "WEB_CONCURRENCY=4" in report.errors[0]


def test_outside_production_the_same_combination_only_warns():
    for environment in ("development", "staging", "test"):
        report = _evaluate(platform_store="memory", environment=environment)
        assert report.ok
        assert any("AI_PLATFORM_STORE=memory" in warning for warning in report.warnings)


def test_environment_name_is_case_insensitive():
    assert not _evaluate(platform_store="memory", environment=" Production ").ok


def test_memory_checkpoints_across_workers_are_refused_in_production_and_warned_elsewhere():
    assert not _evaluate(checkpoint_backend="memory").ok
    report = _evaluate(checkpoint_backend="memory", environment="development")
    assert report.ok and any("AI_CHECKPOINT_BACKEND=memory" in w for w in report.warnings)


def test_a_missing_shared_rate_limit_store_warns_with_the_real_multiplier_but_does_not_block():
    report = _evaluate(rate_limit_storage_uri="  ")
    assert report.ok
    assert any("4x" in warning for warning in report.warnings)


def test_a_nonsense_worker_count_is_treated_as_one():
    assert _evaluate(web_concurrency=0, platform_store="memory").ok
    assert _evaluate(web_concurrency=-3, platform_store="memory").ok


def _settings(**overrides):
    base = dict(WEB_CONCURRENCY=4, RATE_LIMIT_STORAGE_URI="redis://r/1", ENVIRONMENT="production")
    base.update(overrides)
    return SimpleNamespace(**base)


def _ai(**overrides):
    base = dict(AI_PLATFORM_STORE="redis", AI_CHECKPOINT_BACKEND="postgres")
    base.update(overrides)
    return SimpleNamespace(**base)


def test_enforce_raises_in_production_and_logs_loudly(caplog):
    caplog.set_level(logging.WARNING)
    with pytest.raises(RuntimeError, match="AI_PLATFORM_STORE=memory"):
        enforce_worker_topology(_settings(), _ai(AI_PLATFORM_STORE="memory"))
    assert any(record.levelno == logging.CRITICAL and "REFUSING TO START" in record.message for record in caplog.records)


def test_enforce_warns_in_development_and_returns_the_report(caplog):
    caplog.set_level(logging.WARNING)
    report = enforce_worker_topology(_settings(ENVIRONMENT="development"), _ai(AI_PLATFORM_STORE="memory"))
    assert report.ok
    assert any(record.levelno == logging.WARNING and "UNSAFE WORKER TOPOLOGY" in record.message for record in caplog.records)


def test_enforce_is_silent_for_the_default_single_worker_development_setup(caplog):
    caplog.set_level(logging.WARNING)
    enforce_worker_topology(
        _settings(WEB_CONCURRENCY=1, ENVIRONMENT="development", RATE_LIMIT_STORAGE_URI=""),
        _ai(AI_PLATFORM_STORE="memory", AI_CHECKPOINT_BACKEND="memory"),
    )
    assert caplog.records == []


def test_the_real_settings_objects_carry_the_fields_the_guard_reads():
    from app.ai.config import ai_settings
    from app.core.config import settings

    report = enforce_worker_topology(settings, ai_settings)  # default: one worker, so it must pass
    assert settings.WEB_CONCURRENCY == 1 and report.ok


def test_the_lifespan_calls_the_guard():
    """The API refuses to boot through the lifespan, whichever entrypoint started it (also plain uvicorn)."""
    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    assert "enforce_worker_topology(settings, ai_settings)" in source


# --- the entrypoint --------------------------------------------------------------------------


def test_uvicorn_is_started_with_the_requested_workers_and_a_graceful_shutdown_window():
    kwargs = build_uvicorn_kwargs(host="0.0.0.0", port=8000, workers=4, graceful_seconds=20)
    assert kwargs["app"] == "main:app" and kwargs["workers"] == 4
    assert kwargs["proxy_headers"] is True and kwargs["timeout_graceful_shutdown"] == 20
    assert build_uvicorn_kwargs(host="h", port=1, workers=0, graceful_seconds=0)["workers"] == 1


def test_uvicorn_accepts_every_option_the_entrypoint_passes():
    import inspect

    import uvicorn

    accepted = set(inspect.signature(uvicorn.run).parameters) | set(inspect.signature(uvicorn.Config.__init__).parameters)
    assert set(build_uvicorn_kwargs(host="h", port=1, workers=2, graceful_seconds=5)) <= accepted


def test_multiprocess_metrics_directory_is_only_prepared_for_several_workers(tmp_path):
    env: dict = {}
    assert prepare_multiprocess_metrics(1, env) is None and MULTIPROC_ENV not in env

    target = tmp_path / "prom"
    target.mkdir()
    (target / "counter_123.db").write_text("stale")
    (target / "keep.txt").write_text("not a metrics file")
    env = {MULTIPROC_ENV: str(target)}
    assert prepare_multiprocess_metrics(3, env) == str(target)
    assert not (target / "counter_123.db").exists()  # a previous run's numbers must not be summed in
    assert (target / "keep.txt").exists()

    fresh: dict = {}
    created = prepare_multiprocess_metrics(2, fresh)
    assert created and Path(created).is_dir() and fresh[MULTIPROC_ENV] == created


# --- /metrics reports the whole pod, not whichever worker answered -------------------------------

_CHILD = textwrap.dedent(
    """
    import sys
    from app.core.metrics import HTTP_IN_FLIGHT, INGEST_MESSAGES
    for _ in range(3):
        HTTP_IN_FLIGHT.inc()
    INGEST_MESSAGES.labels(outcome="stored").inc(5)
    print("ready", flush=True)
    sys.stdin.readline()  # stay alive: a live process keeps its gauge value
    """
)


async def test_metrics_endpoint_sums_every_worker_process(tmp_path):
    from app.core.metrics import metrics_endpoint

    backend = Path(__file__).resolve().parents[1]
    env = {**os.environ, MULTIPROC_ENV: str(tmp_path), "PYTHONPATH": str(backend)}
    children = [
        subprocess.Popen(
            [sys.executable, "-c", _CHILD],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=env,
            cwd=backend,
        )
        for _ in range(2)
    ]
    try:
        for child in children:
            assert child.stdout.readline().strip() == "ready"
        previous = os.environ.get(MULTIPROC_ENV)
        os.environ[MULTIPROC_ENV] = str(tmp_path)
        try:
            body = (await metrics_endpoint()).body.decode()
        finally:
            if previous is None:
                del os.environ[MULTIPROC_ENV]
            else:
                os.environ[MULTIPROC_ENV] = previous
    finally:
        for child in children:
            child.communicate("\n", timeout=20)

    assert "volta_http_requests_in_flight 6.0" in body  # 2 workers x 3
    assert 'volta_ingest_messages_total{outcome="stored"} 10.0' in body  # 2 workers x 5


async def test_in_flight_gauge_counts_http_requests_and_always_returns_to_its_level():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from prometheus_client import REGISTRY

    from app.core.request_context import RequestContextMiddleware

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    seen: list[float] = []

    @app.get("/probe")
    async def probe() -> dict:
        seen.append(REGISTRY.get_sample_value("volta_http_requests_in_flight"))
        return {}

    @app.get("/boom")
    async def boom() -> dict:
        raise RuntimeError("handler failed")

    before = REGISTRY.get_sample_value("volta_http_requests_in_flight") or 0.0
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t") as client:
        await client.get("/probe")
        await client.get("/boom")  # an exception inside the handler must not leak a count
    assert seen == [before + 1]
    assert REGISTRY.get_sample_value("volta_http_requests_in_flight") == before
