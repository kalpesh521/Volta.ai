"""
Rule engine for alerts and approved automations (Phase 4.1).

Pure Python, no I/O: persistence, the tick worker and the Automation Rule agent live outside this package and talk to it
through `validate_rule` (untrusted dict -> frozen `Rule`), `evaluate` / `evaluate_all` (state transitions only) and
`dry_run` (what would this have done last week).
"""
from app.agents_platform.automation.alert_rules import builtin_alert_rules
from app.agents_platform.automation.dryrun import DryRunReport, dry_run
from app.agents_platform.automation.dsl import (
    DSL_VERSION,
    Action,
    AllOf,
    AnyOf,
    Compare,
    Condition,
    NotifyAction,
    NotOf,
    ProposeDeviceAction,
    Rule,
    Schedule,
    TimeWindow,
    rule_fingerprint,
)
from app.agents_platform.automation.evaluator import (
    DEFAULT_CONFIG,
    EvalConfig,
    Firing,
    evaluate,
    evaluate_all,
)
from app.agents_platform.automation.mapping import (
    ProposedCommand,
    legacy_automation_fields,
    notification_for,
    proposed_command,
)
from app.agents_platform.automation.metrics import METRICS, metric_catalog, resolve_metric
from app.agents_platform.automation.readings import Reading, make_reading, make_series, reading_from_record
from app.agents_platform.automation.state import RuleState
from app.agents_platform.automation.templates import TEMPLATES, RenderedNotification
from app.agents_platform.automation.validation import (
    DeviceSpec,
    RuleProblem,
    RuleValidationError,
    ValidationContext,
    validate_rule,
    validate_rules,
)

__all__ = [
    "Action", "AllOf", "AnyOf", "Compare", "Condition", "DEFAULT_CONFIG", "DSL_VERSION", "DeviceSpec", "DryRunReport",
    "EvalConfig", "Firing", "METRICS", "NotOf", "NotifyAction", "ProposeDeviceAction", "ProposedCommand",
    "Reading", "RenderedNotification", "Rule", "RuleProblem", "RuleState", "RuleValidationError", "Schedule",
    "TEMPLATES", "TimeWindow", "ValidationContext", "builtin_alert_rules", "dry_run", "evaluate", "evaluate_all",
    "legacy_automation_fields", "make_reading", "make_series", "metric_catalog", "notification_for",
    "proposed_command", "reading_from_record", "resolve_metric", "rule_fingerprint", "validate_rule", "validate_rules",
]
