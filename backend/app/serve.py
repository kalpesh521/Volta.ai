"""
Production entrypoint for the API container (Phase 5.2).

    python -m app.serve [--host 0.0.0.0] [--port 8000]

Runs uvicorn with `WEB_CONCURRENCY` worker processes (uvicorn's own supervisor: it restarts a worker
that dies and forwards SIGTERM so each worker finishes its in-flight requests). uvicorn is already a
pinned dependency, so no extra process manager is added.

Before anything starts it runs the worker-topology guard once (instead of once per worker), so a bad
combination fails the container immediately with one clear message. With more than one worker it also
sets up the Prometheus multiprocess directory so `/metrics` reports the whole pod, not one worker.

This module must not import `app.core.metrics` (or anything that imports prometheus_client) before the
multiprocess directory is configured: prometheus_client reads the variable once, at import time.
"""
from __future__ import annotations

import argparse
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger("volta.serve")

MULTIPROC_ENV = "PROMETHEUS_MULTIPROC_DIR"


def prepare_multiprocess_metrics(workers: int, environ: dict | None = None) -> str | None:
    """
    Give prometheus_client a directory for its per-process files when there is more than one worker.
    Stale files from a previous run in the same directory are removed: they would be summed into the
    new run. Returns the directory, or None for a single worker.
    """
    env = os.environ if environ is None else environ
    if workers <= 1:
        return None
    configured = env.get(MULTIPROC_ENV, "").strip()
    directory = Path(configured) if configured else Path(tempfile.mkdtemp(prefix="volta-prom-"))
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.glob("*.db"):
        stale.unlink()
    env[MULTIPROC_ENV] = str(directory)
    return str(directory)


def build_uvicorn_kwargs(*, host: str, port: int, workers: int, graceful_seconds: int) -> dict:
    return {
        "app": "main:app",
        "host": host,
        "port": port,
        "workers": max(1, workers),
        "proxy_headers": True,  # who is trusted to set X-Forwarded-For: FORWARDED_ALLOW_IPS (uvicorn reads it)
        "timeout_graceful_shutdown": max(1, graceful_seconds),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Volta API with WEB_CONCURRENCY uvicorn workers.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    from app.ai.config import ai_settings
    from app.core.config import settings
    from app.core.startup_guards import enforce_worker_topology

    logging.basicConfig(level=settings.LOG_LEVEL)
    workers = max(1, settings.WEB_CONCURRENCY)
    enforce_worker_topology(settings, ai_settings)  # raises in production when the combination is unsafe
    directory = prepare_multiprocess_metrics(workers)

    import uvicorn

    logger.info(
        "starting %d uvicorn worker(s) on %s:%d%s",
        workers,
        args.host,
        args.port,
        f" (metrics dir {directory})" if directory else "",
    )
    uvicorn.run(
        **build_uvicorn_kwargs(
            host=args.host,
            port=args.port,
            workers=workers,
            graceful_seconds=settings.API_GRACEFUL_SHUTDOWN_SECONDS,
        )
    )


if __name__ == "__main__":
    main()
