"""
Question intents and the rule-based first stage of the router cascade.

Rules classify the common questions with zero tokens. Only questions the rules
cannot place confidently are sent to the router LLM.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field


class Intent(StrEnum):
    FULL_OVERVIEW = "full_overview"
    HOME_PROFILE = "home_profile"
    LIVE_OVERVIEW = "live_overview"
    SOLAR_PRODUCTION = "solar_production"
    BATTERY = "battery"
    BACKUP = "backup"
    GRID_IMPORT = "grid_import"
    GRID_EXPORT = "grid_export"
    DEVICE_USAGE = "device_usage"
    APPLIANCE_TIMING = "appliance_timing"
    SAVINGS = "savings"
    USAGE_PATTERN = "usage_pattern"
    WEATHER = "weather"
    DEVICE_CONTROL = "device_control"
    OUT_OF_SCOPE = "out_of_scope"


class ClassificationMethod(StrEnum):
    RULES = "rules"
    LLM = "llm"
    DEFAULT = "default"


INTENT_DESCRIPTIONS: dict[Intent, str] = {
    Intent.FULL_OVERVIEW: "Everything about the home: setup, live state, today, savings and trends together.",
    Intent.HOME_PROFILE: (
        "The home's configured setup: location, panels, inverter, battery size and reserve, "
        "meter, DISCOM, sanctioned load, tariff, monthly bill, goal, tracked appliances."
    ),
    Intent.LIVE_OVERVIEW: "What is happening in the home right now (overall status).",
    Intent.SOLAR_PRODUCTION: "Solar generation now or today, panel output.",
    Intent.BATTERY: "Battery state of charge, charging or discharging and why.",
    Intent.BACKUP: "How long the battery can power the home, outage readiness.",
    Intent.GRID_IMPORT: "Why or how much electricity is bought from the grid.",
    Intent.GRID_EXPORT: "Why or how much electricity is sent to the grid.",
    Intent.DEVICE_USAGE: "Which appliances consume the most power.",
    Intent.APPLIANCE_TIMING: "Whether now is a good time to run a specific appliance.",
    Intent.SAVINGS: "Money saved, bill impact, tariff cost.",
    Intent.USAGE_PATTERN: "Hourly pattern, peaks, when energy is used or produced.",
    Intent.WEATHER: "Weather and its effect on solar output.",
    Intent.DEVICE_CONTROL: "A request to turn on/off, start, stop or schedule a device.",
    Intent.OUT_OF_SCOPE: "Not about this home's energy system.",
}

# Highest first. The primary intent is the highest-priority match.
INTENT_PRIORITY: tuple[Intent, ...] = (
    Intent.DEVICE_CONTROL,
    Intent.APPLIANCE_TIMING,
    Intent.FULL_OVERVIEW,
    Intent.HOME_PROFILE,
    Intent.BACKUP,
    Intent.BATTERY,
    Intent.GRID_IMPORT,
    Intent.GRID_EXPORT,
    Intent.DEVICE_USAGE,
    Intent.SAVINGS,
    Intent.SOLAR_PRODUCTION,
    Intent.USAGE_PATTERN,
    Intent.WEATHER,
    Intent.LIVE_OVERVIEW,
    Intent.OUT_OF_SCOPE,
)

MAX_INTENTS = 3


class ApplianceType(StrEnum):
    WATER_HEATER = "water_heater"
    WASHING_MACHINE = "washing_machine"
    AIR_CONDITIONER = "air_conditioner"
    EV_CHARGER = "ev_charger"
    WATER_PUMP = "water_pump"
    REFRIGERATOR = "refrigerator"


_APPLIANCE_SYNONYMS: dict[ApplianceType, tuple[str, ...]] = {
    ApplianceType.WATER_HEATER: (r"geyser", r"water\s*heater", r"heater", r"boiler", r"immersion rod"),
    ApplianceType.WASHING_MACHINE: (r"washing\s*machine", r"washer", r"laundry"),
    ApplianceType.AIR_CONDITIONER: (r"air\s*condition(?:er|ing)?", r"a\.?c", r"aircon"),
    ApplianceType.EV_CHARGER: (r"ev", r"ev\s*charger", r"electric\s*(?:car|vehicle)", r"car\s*charg\w*"),
    ApplianceType.WATER_PUMP: (r"(?:water\s*)?pump", r"motor"),
    ApplianceType.REFRIGERATOR: (r"fridge", r"refrigerator"),
}

_APPLIANCE_PATTERNS: dict[ApplianceType, re.Pattern[str]] = {
    appliance: re.compile(r"\b(?:" + "|".join(words) + r")\b")
    for appliance, words in _APPLIANCE_SYNONYMS.items()
}


def _rx(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p) for p in patterns)


_RULES: dict[Intent, tuple[re.Pattern[str], ...]] = {
    Intent.DEVICE_CONTROL: _rx(
        r"^(?:please\s+|hey\s+suryaa,?\s+)?(?:can|could|would|will)\s+you\s+"
        r"(?:please\s+)?(?:turn|switch|start|stop|shut|power|run|schedule|set|charge|enable|disable)\b",
        r"^(?:please\s+)?(?:turn|switch|shut|power)\s+(?:it\s+)?(?:on|off|down|up)\b",
        r"^(?:please\s+)?(?:start|stop|run|schedule|set|enable|disable)\s+(?:the|my|charging)\b",
    ),
    Intent.APPLIANCE_TIMING: _rx(
        r"\b(?:should|can|could|may)\s+i\s+(?:run|use|start|turn on|switch on|charge|operate)\b",
        r"\b(?:good|right|best|ideal)\s+time\s+to\s+(?:run|use|start|charge|turn on)\b",
        r"\bwhen\s+should\s+i\s+(?:run|use|start|charge|turn on)\b",
        r"\bis\s+it\s+(?:ok|okay|fine|safe|wise)\s+to\s+(?:run|use|start|charge|turn on)\b",
    ),
    Intent.FULL_OVERVIEW: _rx(
        r"\beverything\b",
        r"\b(?:full|complete|entire|overall|detailed|all)\s+(?:report|details?|overview|summary|picture|"
        r"analysis|info(?:rmation)?|data)\b",
        r"\ball\s+(?:the\s+)?(?:details?|info(?:rmation)?|data)\b",
        r"\btell\s+me\s+about\s+my\s+(?:home|house|system|solar\s+system)\b",
        r"\bsummar(?:y|ise|ize)\s+(?:of\s+)?my\s+(?:home|house|system)\b",
    ),
    Intent.HOME_PROFILE: _rx(
        r"\b(?:home|house|household|system|solar|installation|plant|user)\s+(?:details?|setup|set\s*-?\s*up|"
        r"configuration|config|profile|info(?:rmation)?|specs?|specifications?)\b",
        r"\bmy\s+(?:setup|set\s*-?\s*up|configuration|config|profile|installation)\b",
        r"\bdetails?\s+(?:of|about|for)\s+(?:my|the|this)\s+(?:home|house|system|user|household)\b",
        r"\bhow\s+many\s+(?:solar\s+)?panels\b",
        r"\bpanels?\s+(?:type|count|quantity|brand|make)\b",
        r"\btype\s+of\s+(?:solar\s+)?panels?\b",
        r"\binverter\s+(?:brand|make|model|capacity|size|rating)\b",
        r"\bwhat\s+(?:inverter|battery|panels?|system|meter)\b.*\b(?:do|did)\s+i\s+have\b",
        r"\b(?:battery|system|solar|inverter)\s+(?:size|capacity)\b",
        r"\bsanctioned\s+load\b",
        r"\bdiscom\b",
        r"\b(?:electricity|power)\s+(?:provider|supplier|company|board)\b",
        r"\b(?:monthly|average)\s+(?:electricity\s+)?bill\b",
        r"\bmeter\s+type\b|\btype\s+of\s+meter\b",
        r"\bwhere\s+is\s+my\s+(?:home|house|system)\b|\bmy\s+location\b",
        r"\b(?:my|primary)\s+goal\b",
        r"\bbackup\s+hours?\s+(?:target|setting|configured)\b",
        r"\b(?:tracked|configured|registered)\s+appliances\b",
        r"\bwhich\s+appliances\s+(?:are\s+|did\s+i\s+)?(?:tracked|configured|added|registered)\b",
    ),
    Intent.BACKUP: _rx(
        r"\bbackup\b",
        r"\bhow\s+long\b.*\b(?:last|run|power|keep)\b",
        r"\bpower\s*cut\b",
        r"\boutage\b",
        r"\bload\s*-?\s*shedding\b",
        r"\bblackout\b",
    ),
    Intent.BATTERY: _rx(
        r"\bbatter(?:y|ies)\b",
        r"\bsoc\b",
        r"\bstate\s+of\s+charge\b",
        r"\bdischarg\w*",
    ),
    Intent.GRID_IMPORT: _rx(
        r"\bimport\w*",
        r"\bbuy(?:ing)?\s+(?:power|electricity|energy)\b",
        r"\bfrom\s+the\s+grid\b",
        r"\bgrid\s+(?:usage|consumption|draw)\b",
        r"\bdrawing\s+(?:power\s+)?from\b",
    ),
    Intent.GRID_EXPORT: _rx(
        r"\bexport\w*",
        r"\bsell(?:ing)?\b",
        r"\bto\s+the\s+grid\b",
        r"\bfeed(?:ing)?\s*-?\s*(?:in|back)\b",
    ),
    Intent.DEVICE_USAGE: _rx(
        r"\bappliances?\b",
        r"\bdevices?\b",
        r"\bwhich\b.*\b(?:using|consuming|drawing)\b",
        r"\bpower\s*-?\s*hungry\b",
        r"\bconsum\w*\s+the\s+most\b",
    ),
    Intent.SAVINGS: _rx(
        r"\bsav(?:e|ed|ing|ings)\b",
        r"\bbill\b",
        r"\bcost\w*\b",
        r"\bmoney\b",
        r"₹",
        r"\brupees?\b",
        r"\binr\b",
        r"\btariff\b",
    ),
    Intent.SOLAR_PRODUCTION: _rx(
        r"\bsolar\b",
        r"\bgenerat\w*",
        r"\bpanels?\b",
        r"\bpv\b",
        r"\bproduc\w*",
    ),
    Intent.USAGE_PATTERN: _rx(
        r"\bhourly\b",
        r"\bpeak\b",
        r"\bpattern\w*\b",
        r"\bwhat\s+time\b",
        r"\bthroughout\s+the\s+day\b",
        r"\bby\s+hour\b",
        r"\btrend\w*\b",
    ),
    Intent.WEATHER: _rx(
        r"\bweather\b",
        r"\bclouds?\b",
        r"\bcloudy\b",
        r"\brain\w*\b",
        r"\bsunny\b",
        r"\bsunlight\b",
        r"\btemperature\b",
        r"\bforecast\b",
    ),
    Intent.LIVE_OVERVIEW: _rx(
        r"\bright\s+now\b",
        r"\bcurrent(?:ly)?\b",
        r"\bhappening\b",
        r"\bstatus\b",
        r"\boverview\b",
        r"\bat\s+the\s+moment\b",
        r"\bhow\s+is\s+my\s+(?:home|house|system)\b",
    ),
}

_DOMAIN_HINT = re.compile(
    r"\b(?:energy|power|electric\w*|kw|kwh|solar|grid|battery|home|house|load|usage|inverter)\b"
)

_GENERIC_INTENTS = frozenset({Intent.LIVE_OVERVIEW})
# Composite intents already plan the tools the others would add.
_ABSORBING_INTENTS = frozenset({Intent.DEVICE_CONTROL, Intent.APPLIANCE_TIMING, Intent.FULL_OVERVIEW})


class IntentClassification(BaseModel):
    """Structured output requested from the router LLM."""

    primary_intent: Intent = Field(description="The single best intent for the question.")
    secondary_intents: list[Intent] = Field(
        default_factory=list,
        description="Up to two additional intents needed to answer fully.",
    )
    appliance: ApplianceType | None = Field(
        default=None,
        description="Appliance the user asks about, if any.",
    )


@dataclass(frozen=True)
class IntentMatch:
    intents: tuple[Intent, ...]
    appliance: ApplianceType | None
    confident: bool
    method: ClassificationMethod

    @property
    def primary(self) -> Intent:
        return self.intents[0]


def normalize_question(question: str) -> str:
    return re.sub(r"\s+", " ", question).strip().lower()


def detect_appliance(question: str) -> ApplianceType | None:
    text = normalize_question(question)
    for appliance, pattern in _APPLIANCE_PATTERNS.items():
        if pattern.search(text):
            return appliance
    return None


def order_intents(intents: set[Intent] | list[Intent]) -> tuple[Intent, ...]:
    unique = set(intents)
    ordered = tuple(i for i in INTENT_PRIORITY if i in unique)
    return ordered[:MAX_INTENTS]


def classify_by_rules(question: str) -> IntentMatch:
    text = normalize_question(question)
    appliance = detect_appliance(text)
    matched = {
        intent
        for intent, patterns in _RULES.items()
        if any(pattern.search(text) for pattern in patterns)
    }
    if appliance is not None and Intent.APPLIANCE_TIMING not in matched and re.search(
        r"\bnow\b|\btoday\b|\btonight\b", text
    ) and re.search(r"\b(?:should|can|could|may|good|ok|okay)\b", text):
        matched.add(Intent.APPLIANCE_TIMING)

    for absorbing in INTENT_PRIORITY:
        if absorbing in _ABSORBING_INTENTS and absorbing in matched:
            return IntentMatch((absorbing,), appliance, True, ClassificationMethod.RULES)

    specific = matched - _GENERIC_INTENTS
    if specific:
        matched = specific

    if not matched:
        return IntentMatch(
            (Intent.OUT_OF_SCOPE,) if not _DOMAIN_HINT.search(text) else (Intent.LIVE_OVERVIEW,),
            appliance,
            False,
            ClassificationMethod.RULES,
        )

    ordered = order_intents(matched)
    confident = len(matched) <= MAX_INTENTS
    return IntentMatch(ordered, appliance, confident, ClassificationMethod.RULES)


def from_llm(result: IntentClassification, fallback_appliance: ApplianceType | None) -> IntentMatch:
    intents = [result.primary_intent, *result.secondary_intents]
    if result.primary_intent in _ABSORBING_INTENTS:
        intents = [result.primary_intent]
    ordered = (result.primary_intent,) + tuple(
        i for i in order_intents(intents) if i != result.primary_intent
    )
    return IntentMatch(
        ordered[:MAX_INTENTS],
        result.appliance or fallback_appliance,
        True,
        ClassificationMethod.LLM,
    )
