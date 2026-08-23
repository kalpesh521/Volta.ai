"""
Open-Meteo location helpers.

Docs: https://open-meteo.com/en/docs/geocoding-api
No API key for non-commercial use. Attribution: GeoNames / CC BY 4.0.

Two ways to set the home:

1. Place name → Geocoding search (`/v1/search`) for lat/lon/timezone.
2. Browser GPS → coordinates, then Forecast `timezone=auto` for timezone
   and elevation. Open-Meteo has no reverse-geocode name, so GPS homes
   are labelled "Current location".
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from simulator.config import SimulatorConfig
from simulator.models import LocationRecord, default_location

logger = logging.getLogger("suryaa.geocoding")


def parse_forecast_meta(
    payload: dict[str, Any],
    latitude: float,
    longitude: float,
) -> LocationRecord:
    """Build a GPS location from an Open-Meteo forecast `timezone=auto` response."""
    elevation = payload.get("elevation")
    return LocationRecord(
        name="Current location",
        latitude=float(latitude),
        longitude=float(longitude),
        timezone=str(payload.get("timezone") or "Asia/Kolkata"),
        elevation_m=float(elevation) if elevation is not None else None,
        source="browser-gps",
        data_quality="resolved",
        query=f"{float(latitude):.5f},{float(longitude):.5f}",
    )


def parse_result(row: dict[str, Any], query: str | None = None) -> LocationRecord:
    return LocationRecord(
        name=str(row.get("name") or query or "Unknown"),
        latitude=float(row["latitude"]),
        longitude=float(row["longitude"]),
        timezone=str(row.get("timezone") or "Asia/Kolkata"),
        country=row.get("country"),
        country_code=row.get("country_code"),
        admin1=row.get("admin1"),
        admin2=row.get("admin2"),
        elevation_m=(
            float(row["elevation"]) if row.get("elevation") is not None else None
        ),
        population=row.get("population"),
        location_id=row.get("id"),
        source="open-meteo-geocoding",
        data_quality="resolved",
        query=query,
    )


def apply_location(config: SimulatorConfig, location: LocationRecord) -> SimulatorConfig:
    """Copy resolved geo fields onto config so weather/solar/clock stay in sync."""
    return config.model_copy(
        update={
            "location_name": location.name,
            "latitude": location.latitude,
            "longitude": location.longitude,
            "timezone": location.timezone,
            "location_country": location.country or "",
            "location_country_code": location.country_code or config.location_country_code,
            "location_admin1": location.admin1 or "",
            "location_elevation_m": location.elevation_m
            if location.elevation_m is not None
            else config.location_elevation_m,
            "location_id": location.location_id or 0,
            "location_source": location.source,
            "location_label": location.label(),
        }
    )


class GeocodingClient:
    def __init__(self, config: SimulatorConfig) -> None:
        self.config = config

    async def search(
        self,
        name: str,
        *,
        count: int = 8,
        country_code: str | None = None,
    ) -> list[LocationRecord]:
        query = (name or "").strip()
        if len(query) < 2:
            return []
        params: dict[str, Any] = {
            "name": query,
            "count": max(1, min(count, 100)),
            "language": self.config.geocoding_language,
        }
        code = (country_code or self.config.location_country_code or "").strip()
        if code:
            params["countryCode"] = code.upper()
        timeout = httpx.Timeout(self.config.geocoding_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(self.config.geocoding_base_url, params=params)
            response.raise_for_status()
            payload = response.json()
        rows = payload.get("results") or []
        return [parse_result(row, query) for row in rows if "latitude" in row and "longitude" in row]

    async def resolve(self, name: str | None = None) -> LocationRecord:
        """
        Best match for a place name. On API failure or empty results,
        keep the current config coordinates (or the Pimpri-Chinchwad default).
        """
        query = (name or self.config.location_name or "").strip()
        if not query:
            return self.from_config()
        try:
            matches = await self.search(query)
        except Exception as exc:  # noqa: BLE001 - location must not stop the simulator
            logger.warning("Open-Meteo geocoding failed (%s); using configured location", exc)
            fallback = self.from_config()
            fallback.query = query
            fallback.data_quality = "fallback"
            fallback.source = "fallback"
            return fallback
        if not matches:
            logger.warning("No geocoding matches for %r; using configured location", query)
            fallback = self.from_config()
            fallback.query = query
            fallback.data_quality = "fallback"
            return fallback
        chosen = matches[0]
        logger.info(
            "Resolved location %s → %.4f, %.4f %s",
            chosen.label(),
            chosen.latitude,
            chosen.longitude,
            chosen.timezone,
        )
        return chosen

    async def from_coordinates(self, latitude: float, longitude: float) -> LocationRecord:
        """
        Turn browser GPS coordinates into a LocationRecord.

        Timezone and elevation come from Open-Meteo forecast (`timezone=auto`).
        On API failure the coordinates are still used with the current timezone.
        """
        lat = float(latitude)
        lon = float(longitude)
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            raise ValueError("latitude/longitude out of range")
        params = {
            "latitude": lat,
            "longitude": lon,
            "timezone": "auto",
            "current": "temperature_2m",
        }
        timeout = httpx.Timeout(self.config.open_meteo_timeout_seconds)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(self.config.open_meteo_base_url, params=params)
                response.raise_for_status()
                payload = response.json()
            location = parse_forecast_meta(payload, lat, lon)
            logger.info(
                "GPS location %.4f, %.4f → %s elev=%s",
                lat,
                lon,
                location.timezone,
                location.elevation_m,
            )
            return location
        except Exception as exc:  # noqa: BLE001 - location must not stop the simulator
            logger.warning("Open-Meteo timezone lookup failed for GPS (%s); keeping coords", exc)
            fallback = parse_forecast_meta({}, lat, lon)
            fallback.timezone = self.config.timezone
            fallback.data_quality = "fallback"
            return fallback

    def from_config(self) -> LocationRecord:
        base = default_location()
        return LocationRecord(
            name=self.config.location_name or base.name,
            latitude=self.config.latitude,
            longitude=self.config.longitude,
            timezone=self.config.timezone,
            country=self.config.location_country or base.country,
            country_code=self.config.location_country_code or base.country_code,
            admin1=self.config.location_admin1 or base.admin1,
            elevation_m=self.config.location_elevation_m or base.elevation_m,
            location_id=self.config.location_id or None,
            source=self.config.location_source or "config",
            data_quality="configured",
            query=self.config.location_name,
        )
