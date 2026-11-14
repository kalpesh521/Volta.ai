"""
Regenerates `app/agents_platform/evals/judge/judge_cases.jsonl`. Not collected by the normal run (the name does not
start with `test_`); run it by hand when a scenario changes:

    ./venv/bin/python -m pytest tests/capture_judge_fixtures.py -q

Each scenario ingests one fixed telemetry reading for a fresh home, asks a question through the real v2 pipeline with
a fake model, and records the facts, analytics and freshness exactly as the answer prompt showed them. Nothing
here calls a real model. The reviewer notes (`expect`) are written by hand and are what the judge is told a good
answer covers.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.agents_platform.evals.judge.cases import DEFAULT_CASES_PATH, parse_cases
from app.core.config import settings
from app.modules.assistant.deps import get_structured_llm
from main import app
from tests.assistant_helpers import TZ, FakeStructuredLLM
from tests.test_assistant_api import _home
from tests.test_knowledge_api import MANUAL, _file, _promote
from tests.test_supervisor_api import ask_v2


@dataclass(frozen=True)
class Scenario:
    id: str
    question: str
    expect: tuple[str, ...]
    tags: tuple[str, ...]
    tick: dict = field(default_factory=dict)
    age_minutes: int = 0
    manual: bool = False


SCENARIOS = (
    Scenario(
        "battery-night-discharge",
        "Why is the battery discharging?",
        ("States the battery is discharging and how full it is", "Explains the cause is no solar and the home load", "Does not recommend switching off the refrigerator"),
        ("battery",),
        dict(solar_kw=0.0, load_kw=1.6, charge_kw=0.0, discharge_kw=1.6, soc=48.0, battery_status="discharging", radiation=0.0, cloud=10.0),
    ),
    Scenario(
        "battery-status-charging",
        "What is my current battery status?",
        ("Gives the charge level and that it is charging", "Mentions the charging power"),
        ("battery",),
        dict(solar_kw=3.0, load_kw=1.5, charge_kw=1.5, soc=62.0),
    ),
    Scenario(
        "backup-low-soc",
        "How long will my battery last in a power cut?",
        ("Gives a backup duration taken from the analytics", "Notes the battery is already low", "Suggests reducing non-critical load"),
        ("battery", "backup"),
        dict(solar_kw=0.0, load_kw=1.2, charge_kw=0.0, discharge_kw=1.2, soc=22.0, battery_status="discharging", radiation=0.0),
    ),
    Scenario(
        "grid-import-evening",
        "Why am I importing grid energy?",
        ("States the grid import power", "Explains why the battery and solar are not covering the load"),
        ("grid",),
        dict(solar_kw=0.4, load_kw=2.6, charge_kw=0.0, discharge_kw=0.0, import_kw=2.2, soc=20.0, battery_status="idle", radiation=60.0),
    ),
    Scenario(
        "grid-export-midday",
        "Why am I exporting solar?",
        ("States the export power", "Explains solar exceeds the load and the battery is nearly full", "Suggests using the surplus"),
        ("grid", "solar"),
        dict(solar_kw=4.6, load_kw=1.2, charge_kw=0.3, export_kw=3.1, soc=96.0, battery_status="charging"),
    ),
    Scenario(
        "timing-geyser-surplus",
        "Can I run the geyser now?",
        ("Answers yes or no first", "Bases the answer on solar surplus and the battery", "Does not claim to have switched the geyser on"),
        ("appliance_timing",),
        dict(solar_kw=4.2, load_kw=1.4, charge_kw=1.0, export_kw=1.8, soc=71.0),
    ),
    Scenario(
        "timing-geyser-low-battery",
        "Can I run the geyser now?",
        ("Answers no or not now first", "Gives the reason: low battery and no solar", "Suggests a later time or waiting for solar"),
        ("appliance_timing",),
        dict(solar_kw=0.0, load_kw=1.1, charge_kw=0.0, discharge_kw=1.1, soc=24.0, battery_status="discharging", radiation=0.0),
    ),
    Scenario(
        "overview-now",
        "What is happening in my home now?",
        ("Summarises solar, load, battery and grid now", "Gives the battery charge level"),
        ("overview",),
        dict(solar_kw=3.0, load_kw=1.5, charge_kw=1.5, soc=62.0),
    ),
    Scenario(
        "overview-stale",
        "What is happening in my home now?",
        ("Says plainly the data is old", "Makes any advice conditional on the data being old"),
        ("overview", "stale"),
        dict(solar_kw=3.0, load_kw=1.5, charge_kw=1.5, soc=62.0),
        age_minutes=45,
    ),
    Scenario(
        "solar-today",
        "How much solar did I generate today?",
        ("States today's solar energy in kWh",),
        ("solar", "daily"),
        dict(solar_kw=3.0, load_kw=1.5, charge_kw=1.5, soc=62.0),
    ),
    Scenario(
        "weather-cloudy",
        "Is the cloudy weather hurting output?",
        ("Answers whether cloud is reducing solar", "Uses the cloud cover and radiation values"),
        ("weather", "solar"),
        dict(solar_kw=0.9, load_kw=1.5, charge_kw=0.0, import_kw=0.6, soc=55.0, radiation=180.0, cloud=88.0, battery_status="idle"),
    ),
    Scenario(
        "devices-top-consumer",
        "Which device is consuming the most energy?",
        ("Names the highest-consuming device and its power", "Does not suggest switching off the refrigerator"),
        ("devices",),
        dict(solar_kw=3.0, load_kw=1.5, charge_kw=1.5, soc=62.0),
    ),
    Scenario(
        "profile-system",
        "How many panels do I have?",
        ("States the solar capacity from the household profile", "Does not invent a panel count that is not in the data"),
        ("home_profile",),
        dict(solar_kw=3.0, load_kw=1.5, charge_kw=1.5, soc=62.0),
    ),
    Scenario(
        "document-fault-code",
        "What does fault code E04 mean on my inverter?",
        ("Says E04 means the PV voltage is too high, from the document", "Names the document title", "Tells the user to turn the DC switch off and contact the installer"),
        ("document",),
        dict(solar_kw=3.0, load_kw=1.5, charge_kw=1.5, soc=62.0),
        manual=True,
    ),
    Scenario(
        "savings-today",
        "How much money did I save today?",
        ("States the savings in rupees from the analytics, or says it is unavailable",),
        ("savings", "daily"),
        dict(solar_kw=3.0, load_kw=1.5, charge_kw=1.5, soc=62.0),
    ),
)

_BLOCK = re.compile(r"<(facts|analytics|data_freshness)>\n(.*?)\n</\1>", re.DOTALL)
_INTENTS = re.compile(r"Detected intents: (.+)")


def _extract(prompt: str) -> dict:
    blocks = {name: json.loads(body) for name, body in _BLOCK.findall(prompt)}
    intents = [i.strip() for i in _INTENTS.search(prompt).group(1).split(",")]
    return {"intents": intents, "facts": blocks["facts"], "analytics": blocks["analytics"], "freshness": blocks["data_freshness"]}


@pytest.fixture(scope="module")
def captured():
    rows: dict = {}
    yield rows
    ordered = [rows[s.id] for s in SCENARIOS if s.id in rows]
    if len(ordered) != len(SCENARIOS):
        return  # a scenario failed: never write a partial dataset
    lines = [json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in ordered]
    parse_cases(lines)  # the file must load before it is written
    Path(DEFAULT_CASES_PATH).write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
async def test_capture(scenario: Scenario, client, db_session, tmp_path, monkeypatch, captured: dict):
    fake = FakeStructuredLLM()
    app.dependency_overrides[get_structured_llm] = lambda: fake
    monkeypatch.setattr(settings, "KNOWLEDGE_STORAGE_DIR", str(tmp_path))
    email = f"capture-{scenario.id}@example.com"
    ts = (datetime.now(TZ) - timedelta(minutes=scenario.age_minutes)).replace(microsecond=0)
    headers, _, _ = await _home(client, email, ts=ts, **scenario.tick)
    if scenario.manual:
        await _promote(db_session, email)
        uploaded = await client.post(
            "/knowledge/admin/documents",
            headers=headers,
            data={"doc_type": "inverter_manual", "title": "Growatt fault codes", "brand": "Growatt"},
            files=_file("growatt.txt", MANUAL),
        )
        assert uploaded.status_code == 201, uploaded.text

    await ask_v2(client, headers, scenario.question)

    assert len(fake.calls) == 1, f"{scenario.id}: expected exactly one answer call, got {fake.calls}"
    captured[scenario.id] = {
        "id": scenario.id,
        "tags": list(scenario.tags),
        "question": scenario.question,
        **_extract(fake.last_prompt()),
        "expect": list(scenario.expect),
    }
