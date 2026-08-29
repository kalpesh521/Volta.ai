"""POST simulator ticks to the FastAPI energy ingest endpoint."""

from __future__ import annotations

import logging

import httpx

from simulator.models import TelemetryRecord

logger = logging.getLogger("suryaa.ingest")


def resolve_ingest_url(url: str) -> str:
    cleaned = url.strip().rstrip("/")
    if cleaned.endswith("/energy/ingest"):
        return cleaned
    return f"{cleaned}/energy/ingest"


class IngestClient:
    """
    Best-effort publisher. Generator ticks must not die if the backend is down.
    Failures are logged and rate-limited so 1 Hz dashboard mode does not flood.
    """

    def __init__(self, url: str, token: str, timeout_seconds: float = 3.0) -> None:
        self.url = resolve_ingest_url(url)
        self.token = token.strip()
        self._client = httpx.AsyncClient(timeout=timeout_seconds)
        self._fail_count = 0

    async def publish(self, record: TelemetryRecord) -> None:
        try:
            response = await self._client.post(
                self.url,
                json=record.model_dump(mode="json"),
                headers={
                    "X-Ingest-Token": self.token,
                    "Content-Type": "application/json",
                    "User-Agent": "suryaa-simulator/1.0",
                },
            )
            if response.status_code >= 400:
                self._note_failure(
                    f"HTTP {response.status_code} {response.text[:180]}"
                )
                return
            if self._fail_count:
                logger.info("Backend ingest recovered after %s failures", self._fail_count)
            self._fail_count = 0
        except httpx.HTTPError as exc:
            self._note_failure(str(exc))

    def _note_failure(self, detail: str) -> None:
        self._fail_count += 1
        if self._fail_count <= 3 or self._fail_count % 50 == 0:
            logger.warning("ingest failed (%s): %s", self._fail_count, detail)

    async def aclose(self) -> None:
        await self._client.aclose()
