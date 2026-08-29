"""
Suryaa real-time solar-home telemetry generator.

Run from the repository root:

    python -m simulator.main

Or from this package directory after installing requirements.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from simulator.config import (
    SCENARIOS,
    WEATHER_MODES,
    SimulatorConfig,
    apply_scenario,
)
from simulator.telemetry_generator import TelemetryGenerator

logger = logging.getLogger("suryaa")


def parse_start_time(value: str | None, timezone: str) -> datetime:
    tz = ZoneInfo(timezone)
    if not value:
        return datetime.now(tz=tz)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Suryaa standalone solar-home telemetry simulator",
    )
    parser.add_argument("--interval-seconds", type=int, help="Simulated seconds per reading")
    parser.add_argument(
        "--speed",
        type=float,
        help="Wall-clock speed multiplier (60 = one minute of sim time per second). 0 = as fast as possible.",
    )
    parser.add_argument("--household-id", type=str)
    parser.add_argument("--solar-capacity-kwp", type=float)
    parser.add_argument("--battery-capacity-kwh", type=float)
    parser.add_argument("--start-time", type=str, help="ISO-8601 local or offset datetime")
    parser.add_argument("--scenario", choices=SCENARIOS)
    parser.add_argument("--output-file", type=str)
    parser.add_argument("--weather-mode", choices=WEATHER_MODES)
    parser.add_argument(
        "--ticks",
        type=int,
        default=0,
        help="Stop after N readings (0 = run until Ctrl+C)",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON to stdout")
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Serve the live HTML dashboard at http://127.0.0.1:8765",
    )
    parser.add_argument("--dashboard-port", type=int, default=8765)
    parser.add_argument("--dashboard-host", type=str, default="127.0.0.1")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Do not print every telemetry JSON line to stdout",
    )
    parser.add_argument(
        "--location",
        type=str,
        help="Place name for Open-Meteo geocoding, e.g. 'Pune' or 'Mumbai, India'",
    )
    parser.add_argument("--country-code", type=str, help="ISO country filter, e.g. IN")
    parser.add_argument(
        "--no-geocode",
        action="store_true",
        help="Skip Open-Meteo geocoding and use LATITUDE/LONGITUDE from config",
    )
    parser.add_argument(
        "--ingest-url",
        type=str,
        help="FastAPI base URL or full /energy/ingest URL (e.g. http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--ingest-token",
        type=str,
        help="Shared secret sent as X-Ingest-Token (must match backend INGEST_TOKEN)",
    )
    return parser


def apply_cli_overrides(config: SimulatorConfig, args: argparse.Namespace) -> SimulatorConfig:
    updates: dict = {}
    if args.dashboard:
        # Live dashboard: one reading per real second at the current clock,
        # unless the user overrides interval/speed.
        if args.interval_seconds is None:
            updates["simulation_interval_seconds"] = 1
        if args.speed is None:
            updates["speed"] = 1.0
    if args.interval_seconds is not None:
        updates["simulation_interval_seconds"] = args.interval_seconds
    if args.speed is not None:
        updates["speed"] = args.speed
    if args.household_id:
        updates["household_id"] = args.household_id
    if args.solar_capacity_kwp is not None:
        updates["solar_capacity_kwp"] = args.solar_capacity_kwp
    if args.battery_capacity_kwh is not None:
        updates["battery_capacity_kwh"] = args.battery_capacity_kwh
        updates["battery_usable_capacity_kwh"] = args.battery_capacity_kwh * 0.8
    if args.scenario:
        updates["scenario"] = args.scenario
    if args.output_file:
        updates["output_file"] = args.output_file
    if args.weather_mode:
        updates["weather_mode"] = args.weather_mode
    if args.location:
        updates["location_name"] = args.location
        updates["geocode_on_start"] = True
    if args.country_code:
        updates["location_country_code"] = args.country_code
    if args.no_geocode:
        updates["geocode_on_start"] = False
    if args.ingest_url:
        updates["ingest_url"] = args.ingest_url
    if args.ingest_token:
        updates["ingest_token"] = args.ingest_token
    merged = config.model_copy(update=updates)
    return apply_scenario(merged, merged.scenario)


async def run(
    config: SimulatorConfig,
    start_time: datetime,
    ticks: int,
    pretty: bool,
    quiet: bool = False,
    dashboard: bool = False,
    dashboard_host: str = "127.0.0.1",
    dashboard_port: int = 8765,
    live_clock: bool = True,
) -> None:
    from simulator.dashboard import location_state
    from simulator.clients.geocoding import GeocodingClient, apply_location

    if config.geocode_on_start:
        location = await GeocodingClient(config).resolve(config.location_name)
        config = apply_location(config, location)
        location_state.current_location = location
        logger.info("Home location: %s", location.label())
    else:
        location_state.current_location = GeocodingClient(config).from_config()

    if dashboard:
        from simulator.dashboard.server import start_dashboard_server

        start_dashboard_server(dashboard_host, dashboard_port)
        logger.info(
            "Open the live dashboard: http://%s:%s",
            dashboard_host,
            dashboard_port,
        )

    generator = TelemetryGenerator(config)
    location_state.active_generator = generator
    output_path = generator.open_output()
    tz = ZoneInfo(config.timezone)
    ingest_client = None
    if config.ingest_url.strip():
        from simulator.clients.ingest import IngestClient

        ingest_client = IngestClient(config.ingest_url, config.ingest_token)
        logger.info("Publishing ticks to %s", ingest_client.url)
    logger.info("Writing JSONL to %s", output_path)
    logger.info(
        "household=%s scenario=%s weather=%s interval=%ss speed=%s live_clock=%s",
        config.household_id,
        config.scenario,
        config.weather_mode,
        config.simulation_interval_seconds,
        config.speed,
        live_clock,
    )

    first_ts = datetime.now(tz=tz) if live_clock else start_time
    sim_ts = first_ts
    count = 0
    try:
        await generator.warmup(first_ts)
        while not location_state.stop_requested.is_set() and (ticks <= 0 or count < ticks):
            tz = ZoneInfo(generator.config.timezone)
            if live_clock:
                sim_ts = datetime.now(tz=tz)
            record = await generator.step(sim_ts)
            if ingest_client is not None:
                await ingest_client.publish(record)
            if not quiet:
                if pretty:
                    print(json.dumps(record.model_dump(mode="json"), indent=2, ensure_ascii=False), flush=True)
                else:
                    print(record.model_dump_json(), flush=True)
            count += 1
            if ticks > 0 and count >= ticks:
                break
            if live_clock:
                interval = max(1, config.simulation_interval_seconds)
                next_tick = sim_ts.replace(microsecond=0) + timedelta(seconds=interval)
                delay = (next_tick - datetime.now(tz=tz)).total_seconds()
            else:
                sim_ts = sim_ts + timedelta(seconds=config.simulation_interval_seconds)
                delay = (
                    0.0
                    if config.speed <= 0
                    else config.simulation_interval_seconds / config.speed
                )
            if delay > 0:
                stopped = await asyncio.to_thread(location_state.stop_requested.wait, delay)
                if stopped:
                    break
    except asyncio.CancelledError:
        raise
    except KeyboardInterrupt:
        logger.info("Stopped by user after %s readings", count)
    finally:
        location_state.request_stop()
        generator.close_output()
        if ingest_client is not None:
            await ingest_client.aclose()
        if location_state.stop_requested.is_set():
            logger.info("Simulator stopped after %s readings", count)



def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = build_parser().parse_args(argv)
    config = apply_cli_overrides(SimulatorConfig(), args)
    start = parse_start_time(args.start_time, config.timezone)
    live_clock = args.start_time is None
    try:
        asyncio.run(
            run(
                config,
                start,
                args.ticks,
                args.pretty,
                quiet=args.quiet or args.dashboard,
                dashboard=args.dashboard,
                dashboard_host=args.dashboard_host,
                dashboard_port=args.dashboard_port,
                live_clock=live_clock,
            )
        )
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
