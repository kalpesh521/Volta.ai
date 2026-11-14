"""
Startup guards for the process model (Phase 5.2).

`python -m app.serve` runs `WEB_CONCURRENCY` uvicorn worker processes in one container. Each worker is
a separate Python process with its own memory, exactly like a separate pod. State that is kept in
process memory therefore splits: a kill switch flipped through one worker is not seen by the others, a
cool-down started on one worker does not stop the same user on another, token budgets multiply by the
worker count, a conversation lock does not exclude a request on another worker, and a device action
proposed on one worker cannot be confirmed on another.

The check is a pure function over plain values so it is tested without environment variables.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("volta.startup")


@dataclass(frozen=True)
class TopologyReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def evaluate_worker_topology(
    *,
    web_concurrency: int,
    platform_store: str,
    checkpoint_backend: str,
    rate_limit_storage_uri: str,
    environment: str,
) -> TopologyReport:
    """
    What is unsafe about running `web_concurrency` workers with this configuration.

    Errors (the process must not start): in production, more than one worker while budgets, cool-downs,
    kill switches and conversation locks are per process.
    Warnings: the same combination outside production, and the other per-process stores.
    """
    errors: list[str] = []
    warnings: list[str] = []
    workers = max(1, int(web_concurrency))
    production = environment.strip().lower() == "production"

    if workers > 1 and platform_store != "redis":
        message = (
            f"WEB_CONCURRENCY={workers} with AI_PLATFORM_STORE={platform_store}: token budgets, abuse "
            "cool-downs, feature-flag/kill-switch overrides and conversation locks live in each worker's own "
            "memory and will diverge between workers (budgets multiply by the worker count, a kill switch "
            "reaches only the worker that received the request). Set AI_PLATFORM_STORE=redis."
        )
        (errors if production else warnings).append(message)

    if workers > 1 and checkpoint_backend != "postgres":
        message = (
            f"WEB_CONCURRENCY={workers} with AI_CHECKPOINT_BACKEND={checkpoint_backend}: a device action "
            "proposed on one worker cannot be confirmed on another. Set AI_CHECKPOINT_BACKEND=postgres."
        )
        (errors if production else warnings).append(message)

    if workers > 1 and not rate_limit_storage_uri.strip():
        warnings.append(
            f"WEB_CONCURRENCY={workers} with an empty RATE_LIMIT_STORAGE_URI: each worker counts requests "
            f"separately, so every per-user limit is effectively {workers}x higher. Point it at Redis."
        )

    return TopologyReport(errors=errors, warnings=warnings)


def enforce_worker_topology(settings, ai_settings, *, log: logging.Logger = logger) -> TopologyReport:
    """Log every finding loudly and raise `RuntimeError` when the configuration is not allowed to start."""
    report = evaluate_worker_topology(
        web_concurrency=settings.WEB_CONCURRENCY,
        platform_store=ai_settings.AI_PLATFORM_STORE,
        checkpoint_backend=ai_settings.AI_CHECKPOINT_BACKEND,
        rate_limit_storage_uri=settings.RATE_LIMIT_STORAGE_URI,
        environment=settings.ENVIRONMENT,
    )
    for warning in report.warnings:
        log.warning("UNSAFE WORKER TOPOLOGY: %s", warning)
    if report.errors:
        for error in report.errors:
            log.critical("REFUSING TO START: %s", error)
        raise RuntimeError("; ".join(report.errors))
    return report
