"""Rule-based router (first stage of the cascade) and intent → tool planning."""
import pytest

from app.modules.assistant.domain.intents import (
    ApplianceType,
    ClassificationMethod,
    Intent,
    IntentClassification,
    classify_by_rules,
    from_llm,
)
from app.modules.assistant.graph.planner import build_plan
from app.modules.assistant.tools.registry import ToolName


@pytest.mark.parametrize(
    ("question", "intent", "appliance"),
    [
        ("What is happening in my home right now?", Intent.LIVE_OVERVIEW, None),
        ("How much solar did I generate today?", Intent.SOLAR_PRODUCTION, None),
        ("Why am I importing electricity?", Intent.GRID_IMPORT, None),
        ("Why is my battery discharging?", Intent.BATTERY, None),
        ("Which appliances are consuming the most?", Intent.DEVICE_USAGE, None),
        ("Should I run the washing machine now?", Intent.APPLIANCE_TIMING, ApplianceType.WASHING_MACHINE),
        ("What is my current battery status?", Intent.BATTERY, None),
        ("Why am I exporting power?", Intent.GRID_EXPORT, None),
        ("Should I run the geyser?", Intent.APPLIANCE_TIMING, ApplianceType.WATER_HEATER),
        ("Can I charge my EV now?", Intent.APPLIANCE_TIMING, ApplianceType.EV_CHARGER),
        ("How long will my battery last in a power cut?", Intent.BACKUP, None),
        ("How much money did I save today?", Intent.SAVINGS, None),
        ("When is my peak usage?", Intent.USAGE_PATTERN, None),
        ("Is the cloudy weather hurting output?", Intent.WEATHER, None),
    ],
)
def test_rules_classify_common_questions(question, intent, appliance):
    match = classify_by_rules(question)
    assert match.primary is intent
    assert match.appliance is appliance
    assert match.confident is True
    assert match.method is ClassificationMethod.RULES


@pytest.mark.parametrize(
    "question",
    [
        "Turn off the AC",
        "Switch on the water heater",
        "Can you turn on my geyser?",
        "Please start the washing machine",
    ],
)
def test_device_control_is_detected_and_absorbs_other_intents(question):
    match = classify_by_rules(question)
    assert match.intents == (Intent.DEVICE_CONTROL,)


def test_advice_question_is_not_device_control():
    assert classify_by_rules("Should I turn on the AC now?").primary is Intent.APPLIANCE_TIMING
    assert classify_by_rules("Power usage today?").primary is not Intent.DEVICE_CONTROL


def test_unmatched_questions_are_not_confident():
    assert classify_by_rules("What's the capital of France?").confident is False
    assert classify_by_rules("What's the capital of France?").primary is Intent.OUT_OF_SCOPE
    assert classify_by_rules("Tell me about my energy").confident is False


def test_llm_classification_is_normalised():
    match = from_llm(
        IntentClassification(
            primary_intent=Intent.GRID_IMPORT,
            secondary_intents=[Intent.BATTERY, Intent.GRID_IMPORT, Intent.WEATHER],
        ),
        fallback_appliance=None,
    )
    assert match.intents[0] is Intent.GRID_IMPORT
    assert len(match.intents) == len(set(match.intents)) <= 3
    assert match.method is ClassificationMethod.LLM


def test_plan_union_is_deterministic_and_blocked_intents_have_no_tools():
    plan = build_plan([Intent.BATTERY, Intent.SAVINGS])
    assert ToolName.BATTERY_STATUS in plan.tools
    assert ToolName.DAILY_ENERGY_SUMMARY in plan.tools
    assert list(plan.tools) == sorted(plan.tools, key=list(ToolName).index)
    assert build_plan([Intent.DEVICE_CONTROL]).tools == ()
    assert build_plan([Intent.OUT_OF_SCOPE]).tools == ()
