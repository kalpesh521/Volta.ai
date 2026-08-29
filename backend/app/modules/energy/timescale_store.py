"""
TimescaleDB telemetry history.

Separate engine from Neon (`DATABASE_URL`). Worker ACKs a RabbitMQ message
only after upsert succeeds. Unique (household_id, time) makes retries idempotent.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.modules.energy.schemas import TelemetryRecord

logger = logging.getLogger("volta.timescale")

_HYPERTABLE_SQL = """
SELECT create_hypertable(
    'telemetry_ticks',
    by_range('time'),
    if_not_exists => TRUE
);
"""

_UPSERT_SQL = """
INSERT INTO telemetry_ticks (
    time, household_id, payload,
    solar_power_kw, home_load_power_kw, battery_soc_percent,
    grid_import_power_kw, grid_export_power_kw,
    solar_energy_interval_kwh, home_consumption_interval_kwh,
    grid_import_interval_kwh, grid_export_interval_kwh,
    battery_charge_interval_kwh, battery_discharge_interval_kwh,
    energy_balance_valid
) VALUES (
    :time, :household_id, CAST(:payload AS jsonb),
    :solar_power_kw, :home_load_power_kw, :battery_soc_percent,
    :grid_import_power_kw, :grid_export_power_kw,
    :solar_energy_interval_kwh, :home_consumption_interval_kwh,
    :grid_import_interval_kwh, :grid_export_interval_kwh,
    :battery_charge_interval_kwh, :battery_discharge_interval_kwh,
    :energy_balance_valid
)
ON CONFLICT (household_id, time) DO UPDATE SET
    payload = EXCLUDED.payload,
    solar_power_kw = EXCLUDED.solar_power_kw,
    home_load_power_kw = EXCLUDED.home_load_power_kw,
    battery_soc_percent = EXCLUDED.battery_soc_percent,
    grid_import_power_kw = EXCLUDED.grid_import_power_kw,
    grid_export_power_kw = EXCLUDED.grid_export_power_kw,
    solar_energy_interval_kwh = EXCLUDED.solar_energy_interval_kwh,
    home_consumption_interval_kwh = EXCLUDED.home_consumption_interval_kwh,
    grid_import_interval_kwh = EXCLUDED.grid_import_interval_kwh,
    grid_export_interval_kwh = EXCLUDED.grid_export_interval_kwh,
    battery_charge_interval_kwh = EXCLUDED.battery_charge_interval_kwh,
    battery_discharge_interval_kwh = EXCLUDED.battery_discharge_interval_kwh,
    energy_balance_valid = EXCLUDED.energy_balance_valid
"""


def record_to_params(record: TelemetryRecord) -> dict:
    payload = record.model_dump(mode="json")
    return {
        "time": record.timestamp,
        "household_id": record.household_id,
        "payload": json.dumps(payload, default=str),
        "solar_power_kw": record.solar_power_kw,
        "home_load_power_kw": record.home_load_power_kw,
        "battery_soc_percent": record.battery_soc_percent,
        "grid_import_power_kw": record.grid_import_power_kw,
        "grid_export_power_kw": record.grid_export_power_kw,
        "solar_energy_interval_kwh": record.solar_energy_interval_kwh,
        "home_consumption_interval_kwh": record.home_consumption_interval_kwh,
        "grid_import_interval_kwh": record.grid_import_interval_kwh,
        "grid_export_interval_kwh": record.grid_export_interval_kwh,
        "battery_charge_interval_kwh": record.battery_charge_interval_kwh,
        "battery_discharge_interval_kwh": record.battery_discharge_interval_kwh,
        "energy_balance_valid": record.energy_balance_valid,
    }


def payload_to_record(payload: object) -> TelemetryRecord:
    if isinstance(payload, str):
        payload = json.loads(payload)
    return TelemetryRecord.model_validate(payload)


class TimescaleTelemetryStore:
    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self._engine = engine
        self._sessions: async_sessionmaker[AsyncSession] | None = None

    async def connect(self) -> None:
        if self._engine is None:
            connect_args = {"ssl": "require"} if settings.TIMESCALE_SSL_REQUIRED else {}
            self._engine = create_async_engine(
                settings.TIMESCALE_DATABASE_URL,
                echo=False,
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=5,
                pool_recycle=1800,
                connect_args=connect_args,
            )
        self._sessions = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessions = None

    async def ping(self) -> bool:
        assert self._sessions is not None
        async with self._sessions() as session:
            await session.execute(text("SELECT 1"))
        return True

    async def ensure_schema(self) -> None:
        assert self._engine is not None
        async with self._engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
            await conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS telemetry_ticks (
                        time TIMESTAMPTZ NOT NULL,
                        household_id TEXT NOT NULL,
                        payload JSONB NOT NULL,
                        solar_power_kw DOUBLE PRECISION NOT NULL,
                        home_load_power_kw DOUBLE PRECISION NOT NULL,
                        battery_soc_percent DOUBLE PRECISION NOT NULL,
                        grid_import_power_kw DOUBLE PRECISION NOT NULL,
                        grid_export_power_kw DOUBLE PRECISION NOT NULL,
                        solar_energy_interval_kwh DOUBLE PRECISION NOT NULL,
                        home_consumption_interval_kwh DOUBLE PRECISION NOT NULL,
                        grid_import_interval_kwh DOUBLE PRECISION NOT NULL,
                        grid_export_interval_kwh DOUBLE PRECISION NOT NULL,
                        battery_charge_interval_kwh DOUBLE PRECISION NOT NULL,
                        battery_discharge_interval_kwh DOUBLE PRECISION NOT NULL,
                        energy_balance_valid BOOLEAN NOT NULL,
                        PRIMARY KEY (household_id, time)
                    )
                    """
                )
            )
            try:
                await conn.execute(text(_HYPERTABLE_SQL))
            except Exception:
                await conn.execute(
                    text(
                        "SELECT create_hypertable('telemetry_ticks', 'time', "
                        "if_not_exists => TRUE)"
                    )
                )
            try:
                await conn.execute(
                    text(
                        """
                        ALTER TABLE telemetry_ticks SET (
                            timescaledb.compress,
                            timescaledb.compress_segmentby = 'household_id'
                        )
                        """
                    )
                )
            except Exception as exc:
                logger.debug("compress setting skipped: %s", exc)
            compress_days = max(1, settings.TIMESCALE_COMPRESS_AFTER_DAYS)
            retain_days = max(compress_days + 1, settings.TIMESCALE_RETENTION_DAYS)
            try:
                await conn.execute(
                    text(
                        "SELECT add_compression_policy('telemetry_ticks', "
                        f"INTERVAL '{int(compress_days)} days', if_not_exists => TRUE)"
                    )
                )
                await conn.execute(
                    text(
                        "SELECT add_retention_policy('telemetry_ticks', "
                        f"INTERVAL '{int(retain_days)} days', if_not_exists => TRUE)"
                    )
                )
            except Exception as exc:
                logger.warning("timescale policies skipped: %s", exc)
        logger.info(
            "timescale telemetry_ticks ready (compress=%sd retain=%sd)",
            settings.TIMESCALE_COMPRESS_AFTER_DAYS,
            settings.TIMESCALE_RETENTION_DAYS,
        )

    async def upsert(self, record: TelemetryRecord) -> None:
        assert self._sessions is not None
        async with self._sessions() as session:
            await session.execute(text(_UPSERT_SQL), record_to_params(record))
            await session.commit()

    async def get_latest(self, household_id: str) -> TelemetryRecord | None:
        assert self._sessions is not None
        async with self._sessions() as session:
            result = await session.execute(
                text(
                    """
                    SELECT payload FROM telemetry_ticks
                    WHERE household_id = :household_id
                    ORDER BY time DESC
                    LIMIT 1
                    """
                ),
                {"household_id": household_id},
            )
            row = result.first()
        if row is None:
            return None
        return payload_to_record(row[0])

    async def get_history(self, household_id: str, limit: int = 120) -> list[TelemetryRecord]:
        assert self._sessions is not None
        cap = max(1, min(limit, 2000))
        async with self._sessions() as session:
            result = await session.execute(
                text(
                    """
                    SELECT payload FROM telemetry_ticks
                    WHERE household_id = :household_id
                    ORDER BY time DESC
                    LIMIT :limit
                    """
                ),
                {"household_id": household_id, "limit": cap},
            )
            rows = result.all()
        records = [payload_to_record(row[0]) for row in reversed(rows)]
        return records

    async def fetch_range(
        self,
        household_id: str,
        start: datetime,
        end: datetime,
    ) -> list[TelemetryRecord]:
        assert self._sessions is not None
        async with self._sessions() as session:
            result = await session.execute(
                text(
                    """
                    SELECT payload FROM telemetry_ticks
                    WHERE household_id = :household_id
                      AND time >= :start
                      AND time < :end
                    ORDER BY time ASC
                    """
                ),
                {"household_id": household_id, "start": start, "end": end},
            )
            rows = result.all()
        return [payload_to_record(row[0]) for row in rows]
