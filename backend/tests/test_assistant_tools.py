"""Read-only tools and the request-scoped gateway."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

from app.core.exceptions import EnergyNotFoundError
from app.modules.assistant.tools.gateway import EnergyDataGateway
from app.modules.assistant.tools.registry import ToolName, build_energy_tools
from app.modules.energy.schemas import TelemetryRecord
from app.modules.energy.service import to_live
from tests.assistant_helpers import TZ, make_tick

LIVE_TOOLS = (
    ToolName.LIVE_ENERGY_STATE,
    ToolName.BATTERY_STATUS,
    ToolName.GRID_STATUS,
    ToolName.DEVICE_READINGS,
    ToolName.WEATHER_DATA,
)


class CountingEnergyService:
    def __init__(self, record: TelemetryRecord | None) -> None:
        self.record = record
        self.live_calls = 0

    async def get_live(self, household_id: str, location: str | None = None):
        self.live_calls += 1
        await asyncio.sleep(0.01)
        if self.record is None:
            raise EnergyNotFoundError()
        return to_live(self.record)


def _system() -> SimpleNamespace:
    return SimpleNamespace(household_id="home_tools", location="Pune, India")


async def test_live_projections_share_one_fetch():
    record = TelemetryRecord.model_validate(
        make_tick(datetime(2026, 10, 2, 12, 0, tzinfo=TZ), household_id="home_tools")
    )
    service = CountingEnergyService(record)
    tools = build_energy_tools(EnergyDataGateway(service, _system()))

    results = await asyncio.gather(*(tools[name].ainvoke({}) for name in LIVE_TOOLS))

    assert service.live_calls == 1
    live, battery, grid, devices, weather = results
    assert live["solar_kw"] == 3.0
    assert battery["soc_percent"] == 62.0
    assert grid["status"] == "available"
    assert devices["devices"][0]["name"] == "Air conditioner"
    assert weather["cloud_cover_percent"] == 20.0


async def test_tools_report_unavailable_without_telemetry():
    tools = build_energy_tools(EnergyDataGateway(CountingEnergyService(None), _system()))
    result = await tools[ToolName.BATTERY_STATUS].ainvoke({})
    assert result == {"available": False, "reason": "No telemetry has been received for this home yet."}


def test_tools_never_take_household_id():
    tools = build_energy_tools(EnergyDataGateway(CountingEnergyService(None), _system()))
    assert set(tools) == set(ToolName)
    for tool in tools.values():
        assert "household_id" not in tool.args
