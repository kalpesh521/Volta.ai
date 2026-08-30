"""Fetch GET /energy/{household_id}/profile from the FastAPI backend."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("suryaa.profile")


def resolve_api_base(url: str) -> str:
    cleaned = url.strip().rstrip("/")
    if cleaned.endswith("/energy/ingest"):
        return cleaned[: -len("/energy/ingest")]
    return cleaned


class ProfileClient:
    def __init__(self, api_url: str, token: str, timeout_seconds: float = 8.0) -> None:
        self.base = resolve_api_base(api_url)
        self.token = token.strip()
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    def _headers(self) -> dict[str, str]:
        return {
            "X-Ingest-Token": self.token,
            "User-Agent": "suryaa-simulator/1.0",
        }

    async def fetch(self, household_id: str) -> dict[str, Any]:
        url = f"{self.base}/energy/{household_id}/profile"
        response = await self._client.get(url, headers=self._headers())
        if response.status_code >= 400:
            raise RuntimeError(
                f"onboarding profile HTTP {response.status_code}: {response.text[:240]}"
            )
        data = response.json()
        if not isinstance(data, dict) or "household_id" not in data:
            raise RuntimeError("onboarding profile response was not a profile object")
        return data

    async def fetch_all(self) -> list[dict[str, Any]]:
        url = f"{self.base}/energy/onboarding-profiles"
        response = await self._client.get(url, headers=self._headers())
        if response.status_code >= 400:
            raise RuntimeError(
                f"onboarding profiles HTTP {response.status_code}: {response.text[:240]}"
            )
        data = response.json()
        profiles = data.get("profiles") if isinstance(data, dict) else None
        if not isinstance(profiles, list):
            raise RuntimeError("onboarding profiles response was not a list")
        return [item for item in profiles if isinstance(item, dict) and "household_id" in item]

    async def aclose(self) -> None:
        await self._client.aclose()
