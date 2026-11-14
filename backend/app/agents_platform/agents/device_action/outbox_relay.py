"""
The outbox relay process: delivers device commands that were confirmed but not yet published (tracker 4.3).

    python -m app.agents_platform.agents.device_action.outbox_relay

Run one or several. Rows are claimed with `FOR UPDATE SKIP LOCKED`, so replicas never fight over a row. A confirmed
command is normally delivered immediately by the API itself (`dispatch_now`); this loop is what delivers it when that
did not happen (the API died after the commit, the broker was briefly down) and what retries with a growing pause.
SIGTERM finishes the batch in hand and exits.
"""
from __future__ import annotations

import asyncio
import logging
import signal

from app.agents_platform.agents.device_action.outbox import OutboxRelay
from app.agents_platform.agents.device_action.publisher import default_publisher
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.logging_config import configure_logging
from app.core.metrics import start_worker_metrics_server

logger = logging.getLogger("volta.actions.outbox")


async def run_once(relay: OutboxRelay) -> int:
    async with AsyncSessionLocal() as session:
        result = await relay.dispatch(session)
        await relay.pending_count(session)
        await session.commit()
    return result.handled


async def run_forever(relay: OutboxRelay, stop: asyncio.Event, *, poll_seconds: float) -> None:
    while not stop.is_set():
        try:
            handled = await run_once(relay)
        except Exception:  # noqa: BLE001 - one bad pass (database blip) must not end the process
            logger.exception("outbox pass failed")
            handled = 0
        if handled == 0:  # nothing due: wait, but wake at once on shutdown
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
            except TimeoutError:
                pass


async def _main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    relay = OutboxRelay(default_publisher(), settings)
    logger.info("outbox relay started poll=%.1fs batch=%d", settings.OUTBOX_POLL_SECONDS, settings.OUTBOX_BATCH_SIZE)
    await run_forever(relay, stop, poll_seconds=settings.OUTBOX_POLL_SECONDS)
    logger.info("outbox relay stopped")


def main() -> None:
    configure_logging(settings.LOG_LEVEL, json_logs=settings.LOG_JSON)
    start_worker_metrics_server(settings.WORKER_METRICS_PORT)
    asyncio.run(_main())


if __name__ == "__main__":
    main()
