"""
Rule engine tests (tracker 4.1): DSL validation, edge-triggered evaluation, dry run, fingerprints.

Time convention: series start at T0 = Monday 2026-11-09 12:00 UTC = 17:30 in Asia/Kolkata (UTC+5:30, no DST).
Every series helper uses explicit steps so expected firing indexes can be computed by hand; the comment above each
expectation shows the arithmetic.
"""
from __future__ import annotations

import copy
import json
import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from app.agents_platform.automation import (
    DeviceSpec,
    EvalConfig,
    Reading,
    Rule,
    RuleState,
    RuleValidationError,
    ValidationContext,
    builtin_alert_rules,
    dry_run,
    evaluate,
    evaluate_all,
    legacy_automation_fields,
    make_reading,
    make_series,
    metric_catalog,
    notification_for,
    proposed_command,
    reading_from_record,
    rule_fingerprint,
    validate_rule,
    validate_rules,
)
from app.agents_platform.automation.dsl import AllOf, AnyOf, Compare, NotOf, Schedule
from app.agents_platform.automation.evaluator import eval_condition
from app.agents_platform.automation.metrics import METRICS, read_metric, resolve_metric
from app.agents_platform.automation.templates import TEMPLATES, render_notification
from app.modules.assistant.domain.alerts import alerts_from_home

UTC = timezone.utc
T0 = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)

CTX = ValidationContext.with_devices(
    [
        DeviceSpec("fridge", critical=True),
        DeviceSpec("geyser"),
        DeviceSpec("ev_charger"),
        DeviceSpec("sensor_only", controllable=False),
    ],
    price_available=True,
)


# --- builders -------------------------------------------------------------------------------------------------------


def cmp(metric: str, op: str, value: float) -> dict:
    return {"kind": "compare", "metric": metric, "op": op, "value": value}


def all_of(*items: dict) -> dict:
    return {"kind": "all", "conditions": list(items)}


def any_of(*items: dict) -> dict:
    return {"kind": "any", "conditions": list(items)}


def not_of(item: dict) -> dict:
    return {"kind": "not", "condition": item}


NOTIFY = {"kind": "notify", "channel": "app", "template": "rule_triggered", "params": {}}
SOC_LOW = cmp("battery.soc_percent", "<", 20)


def raw(**over) -> dict:
    base = {"id": "r1", "name": "Low battery", "trigger": copy.deepcopy(SOC_LOW), "cooldown_seconds": 60,
            "actions": [copy.deepcopy(NOTIFY)]}
    base.update(over)
    return base


def mk(**over) -> Rule:
    """A rule straight from the schema (no context checks), for evaluator tests."""
    return Rule.model_validate(raw(**over))


def codes_of(data: dict, ctx: ValidationContext = CTX) -> list[str]:
    with pytest.raises(RuleValidationError) as info:
        validate_rule(data, ctx)
    return info.value.codes


def soc_series(values, step=60, start=T0) -> list[Reading]:
    return make_series(start, step, soc_percent=values)


def replay(rule: Rule, readings: list[Reading], config: EvalConfig | None = None):
    """Run evaluate over readings with now = reading time. Returns (fired indexes, states after each tick)."""
    state = RuleState()
    fired_at, states = [], []
    for index, reading in enumerate(readings):
        args = (rule, state, reading, reading.timestamp) + ((config,) if config else ())
        state, fired = evaluate(*args)
        states.append(state)
        if fired:
            fired_at.append(index)
    return fired_at, states


# =====================================================================================================================
# Validation: every rule has a rejecting and an accepting case
# =====================================================================================================================


class TestSchemaValidation:
    def test_minimal_rule_is_accepted(self):
        rule = validate_rule(raw(), CTX)
        assert rule.id == "r1" and rule.dsl_version == "1.0" and rule.enabled

    def test_non_object_rejected(self):
        with pytest.raises(RuleValidationError) as info:
            validate_rule("not a dict", CTX)  # type: ignore[arg-type]
        assert info.value.codes == ["not_an_object"]

    def test_unknown_top_level_field_rejected(self):
        assert "unknown_field" in codes_of(raw(message="free text please"))

    def test_unknown_field_inside_action_rejected(self):
        action = {**NOTIFY, "body": "hello"}
        assert "unknown_field" in codes_of(raw(actions=[action]))

    def test_missing_required_fields_reported_together(self):
        with pytest.raises(RuleValidationError) as info:
            validate_rule({"name": "x"}, CTX)
        missing = {p.path for p in info.value.problems if p.code == "missing_field"}
        assert {"id", "trigger", "actions"} <= missing

    def test_threshold_must_be_a_number_not_a_string(self):
        assert "wrong_type" in codes_of(raw(trigger=cmp("battery.soc_percent", "<", "20")))

    def test_threshold_must_not_be_bool_or_nan(self):
        assert codes_of(raw(trigger=cmp("battery.soc_percent", "<", True)))
        assert codes_of(raw(trigger=cmp("battery.soc_percent", "<", float("nan"))))

    def test_unsupported_dsl_version_rejected(self):
        assert "invalid_choice" in codes_of(raw(dsl_version="2.0"))
        assert validate_rule(raw(dsl_version="1.0"), CTX)

    def test_operator_must_be_known(self):
        assert "invalid_choice" in codes_of(raw(trigger=cmp("battery.soc_percent", "=~", 20)))

    def test_condition_kind_must_be_known(self):
        assert "invalid_kind" in codes_of(raw(trigger={"kind": "xor", "conditions": []}))
        assert "invalid_kind" in codes_of(raw(trigger={"metric": "battery.soc_percent"}))

    def test_group_needs_two_children(self):
        assert "too_few_items" in codes_of(raw(trigger=all_of(SOC_LOW)))

    def test_error_path_points_at_the_offending_node(self):
        trigger = all_of(SOC_LOW, cmp("battery.soc_percent", "<", "x"))
        with pytest.raises(RuleValidationError) as info:
            validate_rule(raw(trigger=trigger), CTX)
        assert info.value.problems[0].path == "trigger.conditions[1].value"

    def test_name_must_be_plain_text(self):
        assert "name_invalid" in codes_of(raw(name="line\nbreak"))
        assert "name_invalid" in codes_of(raw(name=" padded "))
        assert "name_invalid" in codes_of(raw(name="{braces}"))
        assert validate_rule(raw(name="बैटरी कम (night)"), CTX)

    def test_id_format(self):
        assert "invalid_format" in codes_of(raw(id="has space"))
        assert validate_rule(raw(id="user.rule-1_a"), CTX)

    def test_all_problems_returned_in_one_error_with_codes_paths_messages(self):
        data = raw(cooldown_seconds=5, trigger=cmp("battery.soc_percent", ">", 150), name="bad\nname")
        with pytest.raises(RuleValidationError) as info:
            validate_rule(data, CTX)
        assert len(info.value.problems) >= 1
        for item in info.value.to_dicts():
            assert set(item) == {"code", "path", "message"} and all(item.values())
        data = raw(cooldown_seconds=5, trigger=cmp("battery.soc_percent", ">", 150))
        assert {"cooldown_too_short", "threshold_out_of_range"} <= set(codes_of(data))


class TestMetrics:
    def test_unknown_metric_rejected(self):
        assert "unknown_metric" in codes_of(raw(trigger=cmp("battery.voltage", "<", 3)))
        assert "unknown_metric" in codes_of(raw(trigger=cmp("device.fridge.temperature", "<", 3)))
        assert validate_rule(raw(trigger=cmp("load.power_kw", ">", 3), actions=[NOTIFY]), CTX)

    def test_every_required_metric_exists_with_range_and_unit(self):
        required = {"battery.soc_percent", "battery.temperature_c", "solar.power_kw", "load.power_kw", "grid.import_kw",
                    "grid.export_kw", "grid.available", "battery.discharge_kw", "battery.charge_kw",
                    "time.hour_local", "price.import_inr_per_kwh"}
        assert required == set(METRICS)
        for spec in METRICS.values():
            assert spec.min_value < spec.max_value and spec.unit and spec.description

    @pytest.mark.parametrize(
        "metric,op,value",
        [
            ("battery.soc_percent", ">", 150),
            ("battery.soc_percent", "<", -5),
            ("battery.temperature_c", ">", 500),
            ("solar.power_kw", ">", 1000),
            ("time.hour_local", ">=", 24),
            ("price.import_inr_per_kwh", ">", 1000),
            ("device.geyser.power_kw", ">", 99),
        ],
    )
    def test_threshold_outside_plausible_range_rejected(self, metric, op, value):
        assert "threshold_out_of_range" in codes_of(raw(trigger=cmp(metric, op, value), actions=[NOTIFY]))

    def test_threshold_inside_range_accepted(self):
        for metric, op, value in [("battery.soc_percent", "<", 20), ("battery.temperature_c", ">", 45),
                                  ("time.hour_local", ">=", 22), ("price.import_inr_per_kwh", ">", 9)]:
            assert validate_rule(raw(trigger=cmp(metric, op, value), actions=[NOTIFY]), CTX)

    def test_device_metric_needs_allow_listed_device(self):
        assert "unknown_device" in codes_of(raw(trigger=cmp("device.oven.power_kw", ">", 1), actions=[NOTIFY]))
        assert validate_rule(raw(trigger=cmp("device.geyser.power_kw", ">", 1), actions=[NOTIFY]), CTX)

    def test_bool_metric_only_equality_with_zero_or_one(self):
        for bad in [cmp("grid.available", ">", 0), cmp("grid.available", "==", 2), cmp("grid.available", "<=", 1)]:
            assert "bool_metric_condition" in codes_of(raw(trigger=bad, actions=[NOTIFY]))
        assert validate_rule(raw(trigger=cmp("grid.available", "==", 0), actions=[NOTIFY]), CTX)
        assert validate_rule(raw(trigger=cmp("grid.available", "!=", 1), actions=[NOTIFY]), CTX)

    def test_price_metric_requires_a_tariff(self):
        data = raw(trigger=cmp("price.import_inr_per_kwh", ">", 9), actions=[NOTIFY])
        no_price = ValidationContext(price_available=False)
        assert "metric_unavailable" in codes_of(data, no_price)
        assert validate_rule(data, CTX)

    def test_battery_metrics_require_a_battery(self):
        no_battery = ValidationContext(battery_present=False)
        assert "metric_unavailable" in codes_of(raw(), no_battery)
        assert validate_rule(raw(trigger=cmp("load.power_kw", ">", 2), actions=[NOTIFY]), no_battery)

    def test_catalog_lists_vocabulary_for_the_agent(self):
        ids = {m["id"] for m in metric_catalog()}
        assert "battery.soc_percent" in ids and "device.<device_key>.power_kw" in ids

    def test_reading_metrics_and_unknowns(self):
        reading = make_reading(T0, soc_percent=40, grid_available=False, device_power_kw={"geyser": 2.0})
        local = T0.astimezone(timezone(timedelta(hours=5, minutes=30)))
        assert read_metric("battery.soc_percent", reading, local) == 40
        assert read_metric("grid.available", reading, local) == 0.0
        assert read_metric("device.geyser.power_kw", reading, local) == 2.0
        assert read_metric("device.geyser2.power_kw", reading, local) is None
        assert read_metric("solar.power_kw", reading, local) is None
        assert read_metric("time.hour_local", reading, local) == 17.0
        assert read_metric("nonsense", reading, local) is None
        assert resolve_metric("device.x.power_kw", ["y"]) is None


class TestStructuralLimits:
    @staticmethod
    def nested_not(depth: int) -> dict:
        node = copy.deepcopy(SOC_LOW)
        for _ in range(depth - 1):
            node = not_of(node)
        return node

    def test_depth_four_accepted_depth_five_rejected(self):
        assert validate_rule(raw(trigger=self.nested_not(4)), CTX)
        assert codes_of(raw(trigger=self.nested_not(5))) == ["condition_too_deep"]

    def test_hostile_depth_never_reaches_the_recursive_parser(self):
        assert codes_of(raw(trigger=self.nested_not(5000))) == ["condition_too_deep"]

    def test_depth_limit_also_enforced_by_the_model_itself(self):
        # prescan is bypassed by building the Rule directly; the model validator still refuses
        with pytest.raises(ValueError, match="levels deep"):
            Rule.model_validate(raw(trigger=self.nested_not(5)))

    def test_twelve_nodes_accepted_thirteen_rejected(self):
        twelve = any_of(*[cmp("load.power_kw", ">", float(i)) for i in range(11)])  # 11 leaves + the group
        assert validate_rule(raw(trigger=twelve, actions=[NOTIFY]), CTX)
        thirteen = any_of(*[cmp("load.power_kw", ">", float(i)) for i in range(12)])
        assert codes_of(raw(trigger=thirteen, actions=[NOTIFY])) == ["condition_too_many_nodes"]

    def test_huge_flat_group_is_rejected_cheaply(self):
        giant = any_of(*[cmp("load.power_kw", ">", 1.0)] * 50_000)
        assert codes_of(raw(trigger=giant, actions=[NOTIFY])) == ["condition_too_many_nodes"]


class TestLogicChecks:
    def test_contradiction_rejected(self):
        trigger = all_of(cmp("battery.soc_percent", "<", 10), cmp("battery.soc_percent", ">", 90))
        assert codes_of(raw(trigger=trigger)) == ["condition_never_true"]

    def test_same_bounds_as_or_is_accepted(self):
        trigger = any_of(cmp("battery.soc_percent", "<", 10), cmp("battery.soc_percent", ">", 90))
        assert validate_rule(raw(trigger=trigger), CTX)

    def test_touching_bounds_open_vs_closed(self):
        assert codes_of(raw(trigger=all_of(cmp("battery.soc_percent", "<", 10), cmp("battery.soc_percent", ">=", 10))))
        assert validate_rule(raw(trigger=all_of(cmp("battery.soc_percent", "<=", 10), cmp("battery.soc_percent", ">=", 10))), CTX)

    def test_equality_contradictions(self):
        trigger = all_of(cmp("grid.available", "==", 1), cmp("grid.available", "==", 0))
        assert codes_of(raw(trigger=trigger, actions=[NOTIFY])) == ["condition_never_true"]
        both_not = all_of(cmp("grid.available", "!=", 1), cmp("grid.available", "!=", 0))
        assert codes_of(raw(trigger=both_not, actions=[NOTIFY])) == ["condition_never_true"]

    def test_never_true_inside_range(self):
        assert codes_of(raw(trigger=cmp("battery.soc_percent", "<", 0))) == ["condition_never_true"]
        assert codes_of(raw(trigger=cmp("battery.soc_percent", ">", 100))) == ["condition_never_true"]

    def test_independent_metrics_do_not_contradict(self):
        trigger = all_of(cmp("battery.soc_percent", "<", 10), cmp("load.power_kw", ">", 1))
        assert validate_rule(raw(trigger=trigger), CTX)

    def test_not_contradiction(self):
        trigger = all_of(cmp("battery.soc_percent", "<", 10), not_of(cmp("battery.soc_percent", "<", 50)))
        assert codes_of(raw(trigger=trigger)) == ["condition_never_true"]

    def test_dead_branch_inside_a_satisfiable_any_is_reported(self):
        dead = all_of(cmp("battery.soc_percent", "<", 10), cmp("battery.soc_percent", ">", 90))
        with pytest.raises(RuleValidationError) as info:
            validate_rule(raw(trigger=any_of(cmp("load.power_kw", ">", 3), dead)), CTX)
        problem = info.value.problems[0]
        assert (problem.code, problem.path) == ("condition_branch_never_true", "trigger.conditions[1]")

    @pytest.mark.parametrize(
        "trigger",
        [
            cmp("battery.soc_percent", ">=", 0),
            cmp("battery.soc_percent", "<=", 100),
            any_of(cmp("battery.soc_percent", "<", 50), cmp("battery.soc_percent", ">=", 50)),
            any_of(cmp("grid.available", "==", 0), cmp("grid.available", "==", 1)),
            not_of(cmp("battery.soc_percent", "<", 0)),
        ],
    )
    def test_tautology_rejected(self, trigger):
        assert codes_of(raw(trigger=trigger, actions=[NOTIFY])) == ["condition_always_true"]

    def test_non_trivial_condition_accepted(self):
        assert validate_rule(raw(trigger=cmp("battery.soc_percent", "<", 100)), CTX)
        assert validate_rule(raw(trigger=cmp("battery.soc_percent", ">", 0)), CTX)


class TestTimingAndRepetition:
    def test_cooldown_floor(self):
        assert codes_of(raw(cooldown_seconds=59)) == ["cooldown_too_short"]
        assert validate_rule(raw(cooldown_seconds=60), CTX)

    def test_cooldown_floor_is_configurable(self):
        strict = ValidationContext(min_cooldown_seconds=600)
        assert codes_of(raw(cooldown_seconds=300), strict) == ["cooldown_too_short"]

    def test_unbounded_repetition_rejected(self):
        assert codes_of(raw(max_fires_per_day=500)) == ["repetition_unbounded"]
        assert validate_rule(raw(max_fires_per_day=50), CTX)
        assert "value_out_of_bounds" in codes_of(raw(max_fires_per_day=0))
        assert "value_out_of_bounds" in codes_of(raw(max_fires_per_day=100_000))

    def test_for_seconds_bounds(self):
        assert "value_out_of_bounds" in codes_of(raw(for_seconds=-1))
        assert "value_out_of_bounds" in codes_of(raw(for_seconds=86_401))
        assert validate_rule(raw(for_seconds=3600), CTX)

    def test_hysteresis_bounds_and_effect(self):
        assert "value_out_of_bounds" in codes_of(raw(hysteresis=0.9))
        assert "value_out_of_bounds" in codes_of(raw(hysteresis=-0.1))
        assert validate_rule(raw(hysteresis=0.25), CTX)

    def test_hysteresis_on_condition_without_a_band_is_rejected(self):
        data = raw(trigger=cmp("grid.available", "==", 0), hysteresis=0.2, actions=[NOTIFY])
        assert codes_of(data) == ["hysteresis_ineffective"]
        data = raw(trigger=cmp("battery.soc_percent", "==", 20), hysteresis=0.2)
        assert codes_of(data) == ["hysteresis_ineffective"]
        mixed = raw(trigger=all_of(cmp("grid.available", "==", 0), SOC_LOW), hysteresis=0.2)
        assert validate_rule(mixed, CTX)

    def test_duplicate_rule_id_rejected(self):
        ctx = ValidationContext.with_devices([], existing_rule_ids=frozenset({"r1"}))
        assert codes_of(raw(), ctx) == ["duplicate_rule_id"]
        assert validate_rule(raw(id="r2"), ctx)

    def test_duplicate_ids_inside_a_batch(self):
        with pytest.raises(RuleValidationError) as info:
            validate_rules([raw(id="a"), raw(id="a")], CTX)
        assert info.value.problems[0].path == "rules[1].id"
        assert len(validate_rules([raw(id="a"), raw(id="b")], CTX)) == 2

    def test_timezone(self):
        assert "timezone_invalid" in codes_of(raw(timezone="Mars/Olympus"))
        assert validate_rule(raw(timezone="Asia/Kolkata"), CTX)
        home = ValidationContext(home_timezone="Asia/Kolkata")
        assert codes_of(raw(timezone="UTC"), home) == ["timezone_mismatch"]


class TestScheduleValidation:
    def test_valid_schedule_accepted_and_weekdays_canonicalised(self):
        rule = validate_rule(raw(schedule={"windows": [{"start": "22:00", "end": "06:00"}],
                                           "weekdays": ["sat", "mon", "mon"]}), CTX)
        assert rule.schedule.weekdays == ["mon", "sat"]

    def test_bad_window_rejected(self):
        for window in [{"start": "25:00", "end": "06:00"}, {"start": "9am", "end": "06:00"}]:
            assert "invalid_format" in codes_of(raw(schedule={"windows": [window]}))
        assert "schedule_window_empty" in codes_of(raw(schedule={"windows": [{"start": "08:00", "end": "08:00"}]}))

    def test_too_many_windows_rejected(self):
        windows = [{"start": f"0{i}:00", "end": f"0{i}:30"} for i in range(5)]
        assert "too_many_items" in codes_of(raw(schedule={"windows": windows}))

    def test_schedule_that_restricts_nothing_rejected(self):
        assert codes_of(raw(schedule={})) == ["schedule_no_restriction"]
        assert validate_rule(raw(schedule={"weekdays": ["mon"]}), CTX)

    def test_unknown_weekday_rejected(self):
        assert "invalid_choice" in codes_of(raw(schedule={"weekdays": ["funday"]}))


class TestActionValidation:
    def test_action_list_bounds(self):
        assert "too_few_items" in codes_of(raw(actions=[]))
        four = [{**NOTIFY, "channel": "app"}, {**NOTIFY, "channel": "push"},
                {"kind": "propose_device_action", "action": "turn_on", "device": "geyser"},
                {"kind": "propose_device_action", "action": "turn_on", "device": "ev_charger"}]
        assert "too_many_items" in codes_of(raw(actions=four))
        assert validate_rule(raw(actions=four[:3]), CTX)

    def test_set_flag_is_not_an_action(self):
        assert "invalid_kind" in codes_of(raw(actions=[{"kind": "set_flag", "flag": "away"}]))

    def test_free_text_notification_is_not_expressible(self):
        assert "unknown_field" in codes_of(raw(actions=[{**NOTIFY, "message": "Click evil.example"}]))
        assert "unknown_template" in codes_of(raw(actions=[{**NOTIFY, "template": "Click evil.example"}]))

    def test_template_params_are_typed_and_exact(self):
        low = {"kind": "notify", "channel": "push", "template": "battery_low"}
        assert "template_params_invalid" in codes_of(raw(actions=[low]))
        assert "template_params_invalid" in codes_of(raw(actions=[{**low, "params": {"threshold_percent": 20, "x": 1}}]))
        assert "template_param_out_of_range" in codes_of(raw(actions=[{**low, "params": {"threshold_percent": 150}}]))
        assert "wrong_type" in codes_of(raw(actions=[{**low, "params": {"threshold_percent": "20"}}]))
        assert validate_rule(raw(actions=[{**low, "params": {"threshold_percent": 20}}]), CTX)

    def test_channel_closed_set(self):
        assert "invalid_choice" in codes_of(raw(actions=[{**NOTIFY, "channel": "sms"}]))
        assert validate_rule(raw(actions=[{**NOTIFY, "channel": "push"}]), CTX)

    def test_template_may_not_misdescribe_the_trigger(self):
        battery_low = {"kind": "notify", "channel": "app", "template": "battery_low", "params": {"threshold_percent": 20}}
        data = raw(trigger=cmp("load.power_kw", ">", 3), actions=[battery_low])
        assert codes_of(data) == ["template_metric_mismatch"]
        assert validate_rule(raw(actions=[battery_low]), CTX)

    def propose(self, action, device, **extra):
        return {"kind": "propose_device_action", "action": action, "device": device, **extra}

    def test_critical_device_turn_off_rejected(self):
        assert codes_of(raw(actions=[self.propose("turn_off", "fridge")])) == ["critical_device_turn_off"]
        assert validate_rule(raw(actions=[self.propose("turn_off", "geyser")]), CTX)

    def test_critical_device_set_to_zero_rejected_but_other_levels_and_on_allowed(self):
        assert codes_of(raw(actions=[self.propose("set_level", "fridge", level_percent=0)])) == ["critical_device_turn_off"]
        assert validate_rule(raw(actions=[self.propose("set_level", "fridge", level_percent=50)]), CTX)
        assert validate_rule(raw(actions=[self.propose("turn_on", "fridge")]), CTX)

    def test_unknown_and_uncontrollable_devices(self):
        assert codes_of(raw(actions=[self.propose("turn_on", "oven")])) == ["unknown_device"]
        assert codes_of(raw(actions=[self.propose("turn_on", "sensor_only")])) == ["device_not_controllable"]
        assert codes_of(raw(actions=[self.propose("turn_on", "geyser")]), ValidationContext()) == ["unknown_device"]

    def test_action_parameter_rules(self):
        assert "param_required" in codes_of(raw(actions=[self.propose("set_level", "geyser")]))
        assert "param_not_applicable" in codes_of(raw(actions=[self.propose("turn_on", "geyser", level_percent=50)]))
        assert "param_not_applicable" in codes_of(raw(actions=[self.propose("turn_off", "geyser", duration_minutes=30)]))
        assert "value_out_of_bounds" in codes_of(raw(actions=[self.propose("set_level", "geyser", level_percent=101)]))
        assert validate_rule(raw(actions=[self.propose("set_level", "geyser", level_percent=60, duration_minutes=30)]), CTX)

    def test_duplicate_and_conflicting_actions(self):
        on = self.propose("turn_on", "geyser")
        assert codes_of(raw(actions=[on, dict(on)])) == ["duplicate_action"]
        assert codes_of(raw(actions=[on, self.propose("turn_off", "geyser")])) == ["conflicting_actions"]
        assert validate_rule(raw(actions=[on, self.propose("turn_on", "ev_charger")]), CTX)

    def test_unsupported_device_key_characters_rejected(self):
        assert "invalid_format" in codes_of(raw(actions=[self.propose("turn_on", "../etc")]))


# =====================================================================================================================
# JSON round-trip, immutability, fingerprint
# =====================================================================================================================


FULL = raw(
    trigger=all_of(SOC_LOW, any_of(cmp("grid.available", "==", 0), not_of(cmp("solar.power_kw", ">", 1.5)))),
    for_seconds=120, cooldown_seconds=600, hysteresis=0.1, max_fires_per_day=5, timezone="Asia/Kolkata",
    schedule={"windows": [{"start": "22:00", "end": "06:00"}], "weekdays": ["mon", "tue"]},
    actions=[{"kind": "notify", "channel": "push", "template": "battery_low", "params": {"threshold_percent": 20}},
             {"kind": "propose_device_action", "action": "turn_off", "device": "geyser"}],
)


class TestRoundTripAndFingerprint:
    def test_json_round_trip_is_lossless(self):
        rule = validate_rule(FULL, CTX)
        text = rule.model_dump_json()
        again = validate_rule(json.loads(text), CTX)
        assert again == rule
        assert Rule.model_validate_json(text) == rule
        assert again.model_dump_json() == text

    def test_rules_are_immutable(self):
        rule = validate_rule(raw(), CTX)
        with pytest.raises(Exception):
            rule.name = "changed"  # type: ignore[misc]

    def test_fingerprint_is_stable_and_hex(self):
        a, b = validate_rule(FULL, CTX), validate_rule(copy.deepcopy(FULL), CTX)
        assert rule_fingerprint(a) == rule_fingerprint(b)
        assert len(rule_fingerprint(a)) == 64 and int(rule_fingerprint(a), 16) >= 0

    def test_fingerprint_ignores_key_order_int_vs_float_and_enabled(self):
        base = validate_rule(raw(), CTX)
        shuffled = dict(reversed(list(raw().items())))
        assert rule_fingerprint(validate_rule(shuffled, CTX)) == rule_fingerprint(base)
        as_float = raw(trigger=cmp("battery.soc_percent", "<", 20.0))
        assert rule_fingerprint(validate_rule(as_float, CTX)) == rule_fingerprint(base)
        assert rule_fingerprint(validate_rule(raw(enabled=False), CTX)) == rule_fingerprint(base)

    @pytest.mark.parametrize(
        "change",
        [
            {"id": "r2"},
            {"name": "Other name"},
            {"trigger": cmp("battery.soc_percent", "<", 19)},
            {"trigger": cmp("battery.soc_percent", "<=", 20)},
            {"for_seconds": 1},
            {"cooldown_seconds": 61},
            {"hysteresis": 0.05},
            {"max_fires_per_day": 9},
            {"timezone": "UTC"},
            {"schedule": {"weekdays": ["mon"]}},
            {"actions": [{**NOTIFY, "channel": "push"}]},
            {"actions": [{"kind": "propose_device_action", "action": "turn_on", "device": "geyser"}]},
        ],
    )
    def test_fingerprint_changes_when_behaviour_changes(self, change):
        assert rule_fingerprint(validate_rule(raw(**change), CTX)) != rule_fingerprint(validate_rule(raw(), CTX))

    def test_fingerprint_sensitive_to_nested_edit(self):
        a = copy.deepcopy(FULL)
        b = copy.deepcopy(FULL)
        b["trigger"]["conditions"][1]["conditions"][1]["condition"]["value"] = 1.6
        assert rule_fingerprint(validate_rule(a, CTX)) != rule_fingerprint(validate_rule(b, CTX))


# =====================================================================================================================
# Evaluator: edge-triggered semantics
# =====================================================================================================================


class TestEdgeTrigger:
    def test_fires_once_when_condition_becomes_true_and_not_again_while_true(self):
        # 60 s steps: 50 (idx0 false), then 15 from idx1 on: only idx1 fires.
        fired, _ = replay(mk(), soc_series([50, 15, 15, 15, 15, 15]))
        assert fired == [1]

    def test_no_refire_when_cooldown_elapses_while_still_true(self):
        fired, _ = replay(mk(cooldown_seconds=120), soc_series([15] * 10))
        assert fired == [0]

    def test_refires_after_clear_and_cooldown(self):
        # cooldown 300 s, step 60 s. Fire idx1 (t=60). Clear idx2 (t=120), cooldown until t=360.
        # idx3 (t=180) true again but still cooling -> no fire. Cooldown over at idx6 (t=360). idx9 true -> fires.
        series = soc_series([50, 15, 50, 15, 50, 50, 50, 50, 50, 15])
        fired, states = replay(mk(cooldown_seconds=300), series)
        assert fired == [1, 9]
        assert states[2].phase == "cooling" and states[2].cooling_until == T0.timestamp() + 360

    def test_clear_after_cooldown_already_elapsed_goes_straight_back_to_inactive(self):
        # cooldown 60 s, step 60 s: fire t=0, clear t=60 (cooldown ends exactly then) -> inactive, re-trigger at t=120.
        fired, states = replay(mk(cooldown_seconds=60), soc_series([15, 50, 15]))
        assert fired == [0, 2] and states[1].phase == "inactive"

    def test_never_fires_twice_within_cooldown_even_if_cleared_and_retriggered(self):
        fired, _ = replay(mk(cooldown_seconds=600), soc_series([15, 50, 15, 50, 15, 50, 15], step=60))
        assert fired == [0]

    def test_cooldown_counts_from_firing_not_from_clearing(self):
        # for_seconds 0, cooldown 300. Fire t=0; stays true until t=240, clears t=300 -> cooldown (300) already over.
        series = soc_series([15, 15, 15, 15, 15, 50, 15], step=60)
        fired, states = replay(mk(cooldown_seconds=300), series)
        assert fired == [0, 6] and states[5].phase == "inactive"

    def test_condition_true_at_first_reading_fires_immediately(self):
        fired, _ = replay(mk(), soc_series([5]))
        assert fired == [0]

    def test_firing_carries_actions_fingerprint_and_observations(self):
        rule = mk()
        state, fired = evaluate(rule, RuleState(), make_reading(T0, soc_percent=12), T0)
        (firing,) = fired
        assert firing.rule_id == "r1" and firing.fired_at == T0
        assert firing.fingerprint == rule_fingerprint(rule)
        assert firing.actions == tuple(rule.actions)
        assert firing.observed == {"battery.soc_percent": 12.0}
        assert state.phase == "firing" and state.fires_today == 1

    def test_disabled_rule_never_fires_and_resets_state(self):
        rule = mk(enabled=False)
        busy = RuleState(phase="firing", fired_at=1.0)
        state, fired = evaluate(rule, busy, make_reading(T0, soc_percent=1), T0)
        assert fired == [] and state == RuleState()

    def test_naive_now_is_taken_as_utc(self):
        state, fired = evaluate(mk(), RuleState(), make_reading(T0, soc_percent=1), T0.replace(tzinfo=None))
        assert len(fired) == 1 and state.fired_at == T0.timestamp()


class TestForSeconds:
    def test_condition_must_hold_for_the_whole_duration(self):
        # for 300 s, step 60 s: pending at idx0 (t=0); t-since >= 300 first at idx5.
        fired, states = replay(mk(for_seconds=300), soc_series([15] * 8))
        assert fired == [5]
        assert states[0].phase == "pending" and states[4].phase == "pending"

    def test_pending_resets_when_the_condition_drops(self):
        # pending idx0, drop idx3 -> inactive; pending again idx4 (since t=240); fires when t=540 (idx9).
        fired, states = replay(mk(for_seconds=300), soc_series([15, 15, 15, 50, 15, 15, 15, 15, 15, 15]))
        assert states[3].phase == "inactive"
        assert fired == [9]

    def test_short_blip_never_fires(self):
        fired, _ = replay(mk(for_seconds=300), soc_series([15, 15, 50, 15, 15, 50, 15, 15, 50]))
        assert fired == []

    def test_for_seconds_with_hysteresis_survives_wobble_inside_the_band(self):
        # soc<20, hysteresis 0.25 -> band 5: 21 is inside the band (<25) so the pending timer keeps running.
        series = soc_series([15, 21, 19, 15], step=60)
        assert replay(mk(for_seconds=180, hysteresis=0.25), series)[0] == [3]
        assert replay(mk(for_seconds=180), series)[0] == []


class TestHysteresis:
    FLAP = [15, 21, 19, 21, 19, 26, 19]

    def test_without_hysteresis_a_flapping_value_refires(self):
        fired, _ = replay(mk(), soc_series(self.FLAP))
        assert fired == [0, 2, 4, 6]

    def test_hysteresis_holds_state_until_value_clears_the_band(self):
        # band = max(0.25*20, 1) = 5: holds while <25; 26 at idx5 clears (cooldown over) so idx6 re-fires.
        fired, states = replay(mk(hysteresis=0.25), soc_series(self.FLAP))
        assert fired == [0, 6]
        assert states[4].phase == "firing" and states[5].phase == "inactive"

    def test_entering_uses_the_plain_threshold(self):
        # 21 is inside the band but a rule that is not active must not arm on it.
        fired, _ = replay(mk(hysteresis=0.25), soc_series([50, 21, 22, 23]))
        assert fired == []

    def test_greater_than_direction(self):
        rule = mk(trigger=cmp("load.power_kw", ">", 4.0), hysteresis=0.25, actions=[NOTIFY])
        series = make_series(T0, 60, load_kw=[5, 3.5, 5.5, 2.9, 5])
        # band = 1.0: holds while load > 3.0. 3.5 holds, 2.9 clears, final 5 re-arms and fires.
        assert replay(rule, series)[0] == [0, 4]

    def test_negation_flips_the_direction_of_the_band(self):
        direct = mk(hysteresis=0.25)
        negated = mk(trigger=not_of(cmp("battery.soc_percent", ">=", 20)), hysteresis=0.25)
        series = soc_series(self.FLAP + [24.9, 19, 40, 10])
        assert replay(negated, series)[0] == replay(direct, series)[0]

    def test_equality_leaf_is_never_widened(self):
        rule = mk(trigger=all_of(cmp("grid.available", "==", 0), SOC_LOW), hysteresis=0.25, actions=[NOTIFY])
        up = make_series(T0, 60, grid_available=[False, True, False], soc_percent=[15, 15, 15])
        assert replay(rule, up)[0] == [0, 2]  # the grid flipping back clears at once; only SOC got a band


class TestUnknownData:
    def test_none_never_fires_or_arms(self):
        fired, states = replay(mk(), soc_series([None] * 5))
        assert fired == [] and all(s.phase == "inactive" for s in states)
        fired, states = replay(mk(for_seconds=60), soc_series([None] * 5))
        assert fired == [] and all(s.phase == "inactive" for s in states)

    def test_none_does_not_reset_a_pending_timer(self):
        # for 300 s. pending since t=0; None at idx2,3 holds; idx4 (t=240) true, idx5 (t=300): 300-0 >= 300 -> fires.
        fired, states = replay(mk(for_seconds=300), soc_series([15, 15, None, None, 15, 15, 15]))
        assert states[2].phase == states[3].phase == "pending"
        assert fired == [5]

    def test_none_does_not_end_a_firing(self):
        # if None counted as "cleared", idx3 would fire a second time.
        fired, states = replay(mk(), soc_series([15, None, None, 15]))
        assert fired == [0] and states[2].phase == "firing"

    def test_pending_timer_does_not_survive_blindness_beyond_the_stale_limit(self):
        # stale limit 120 s. Last confirmed true at t=60; None at t=120 (gap 60, hold), t=180 (120, hold),
        # t=240 (gap 180 > 120) -> pending dropped. Re-armed at idx5 (t=300), fires t=600 (idx10), not at idx5.
        config = EvalConfig(stale_limit_seconds=120)
        series = soc_series([15, 15, None, None, None, 15, 15, 15, 15, 15, 15])
        fired, states = replay(mk(for_seconds=300), series, config)
        assert states[3].phase == "pending"
        assert states[4].phase == "inactive"
        assert fired == [10]

    def test_true_readings_after_a_long_gap_restart_the_timer(self):
        # sparse (not None) readings: 0 s, 60 s, then 1000 s (gap 940 > 600): continuity unproven, since=1000.
        stamps = [0, 60, 1000, 1060, 1300, 1400]
        series = [make_reading(T0 + timedelta(seconds=s), soc_percent=15) for s in stamps]
        fired, _ = replay(mk(for_seconds=300), series)
        assert fired == [4]  # 1300 - 1000 = 300

    def test_reading_older_than_the_stale_limit_is_unknown(self):
        old = make_reading(T0, soc_percent=1)
        state, fired = evaluate(mk(), RuleState(), old, T0 + timedelta(seconds=700))
        assert fired == [] and state.phase == "inactive"
        state, fired = evaluate(mk(), RuleState(), old, T0 + timedelta(seconds=599))
        assert len(fired) == 1

    def test_stale_reading_still_knows_the_clock(self):
        rule = mk(trigger=cmp("time.hour_local", ">=", 17), actions=[NOTIFY])
        old = make_reading(T0 - timedelta(hours=3))
        assert len(evaluate(rule, RuleState(), old, T0)[1]) == 1

    def test_three_valued_logic_truth_table(self):
        soc_lt = Compare(metric="battery.soc_percent", op="<", value=20)
        load_gt = Compare(metric="load.power_kw", op=">", value=3)
        known = {"battery.soc_percent": 10.0, "load.power_kw": None}
        read = known.get
        assert eval_condition(soc_lt, read) is True
        assert eval_condition(load_gt, read) is None
        assert eval_condition(AllOf(conditions=[soc_lt, load_gt]), read) is None
        assert eval_condition(AnyOf(conditions=[soc_lt, load_gt]), read) is True
        assert eval_condition(NotOf(condition=load_gt), read) is None
        low_false = {"battery.soc_percent": 50.0, "load.power_kw": None}.get
        assert eval_condition(AllOf(conditions=[soc_lt, load_gt]), low_false) is False
        assert eval_condition(AnyOf(conditions=[soc_lt, load_gt]), low_false) is None

    def test_none_inside_all_with_a_false_sibling_is_false_and_clears(self):
        rule = mk(trigger=all_of(SOC_LOW, cmp("load.power_kw", ">", 3)), actions=[NOTIFY])
        series = make_series(T0, 60, soc_percent=[15, 50], load_kw=[5, None])
        _, states = replay(rule, series)
        assert states[0].phase == "firing" and states[1].phase in {"cooling", "inactive"}

    def test_non_finite_values_become_unknown(self):
        reading = make_reading(T0, soc_percent=float("nan"), solar_kw=math.inf)
        assert reading.soc_percent is None and reading.solar_kw is None
        assert evaluate(mk(), RuleState(), reading, T0)[1] == []
        assert evaluate(mk(trigger=not_of(SOC_LOW), actions=[NOTIFY]), RuleState(), reading, T0)[1] == []


class TestSchedule:
    IST_NIGHT = Schedule.model_validate({"windows": [{"start": "22:00", "end": "06:00"}], "weekdays": ["mon"]})

    @staticmethod
    def local(y, m, d, hh, mm):
        from zoneinfo import ZoneInfo

        return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("Asia/Kolkata"))

    def test_midnight_wrap_belongs_to_the_day_the_window_starts(self):
        s = self.IST_NIGHT  # Monday 2026-11-09 only
        assert s.contains(self.local(2026, 11, 9, 22, 0))  # start inclusive
        assert s.contains(self.local(2026, 11, 9, 23, 59))
        assert s.contains(self.local(2026, 11, 10, 0, 0))  # Tuesday 00:00 is part of Monday's window
        assert s.contains(self.local(2026, 11, 10, 5, 59))
        assert not s.contains(self.local(2026, 11, 10, 6, 0))  # end exclusive
        assert not s.contains(self.local(2026, 11, 9, 5, 59))  # Monday morning belongs to Sunday's window
        assert not s.contains(self.local(2026, 11, 9, 12, 0))
        assert not s.contains(self.local(2026, 11, 10, 22, 30))  # Tuesday evening: not an allowed weekday

    def test_plain_window_and_weekdays(self):
        s = Schedule.model_validate({"windows": [{"start": "09:00", "end": "17:00"}],
                                     "weekdays": ["mon", "tue", "wed", "thu", "fri"]})
        assert s.contains(self.local(2026, 11, 9, 9, 0)) and s.contains(self.local(2026, 11, 9, 16, 59))
        assert not s.contains(self.local(2026, 11, 9, 17, 0))
        assert not s.contains(self.local(2026, 11, 14, 10, 0))  # Saturday

    def test_weekdays_only_means_all_day(self):
        s = Schedule.model_validate({"weekdays": ["sat", "sun"]})
        assert s.contains(self.local(2026, 11, 14, 3, 0)) and not s.contains(self.local(2026, 11, 9, 3, 0))

    def test_multiple_windows(self):
        s = Schedule.model_validate({"windows": [{"start": "06:00", "end": "08:00"}, {"start": "18:00", "end": "20:00"}]})
        assert s.contains(self.local(2026, 11, 9, 7, 0)) and s.contains(self.local(2026, 11, 9, 19, 0))
        assert not s.contains(self.local(2026, 11, 9, 12, 0))

    def test_kolkata_has_no_dst_so_the_offset_is_constant_all_year(self):
        s = Schedule.model_validate({"windows": [{"start": "17:30", "end": "17:31"}]})
        for month in (1, 3, 4, 6, 10, 11):  # around every DST changeover date in other zones
            utc = datetime(2026, month, 15, 12, 0, tzinfo=UTC)
            assert s.contains(utc.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata")))

    def test_evaluator_uses_the_homes_timezone_not_utc(self):
        # T0 = 12:00 UTC = 17:30 IST.
        in_ist = mk(schedule={"windows": [{"start": "17:00", "end": "18:00"}]})
        in_utc_only = mk(schedule={"windows": [{"start": "12:00", "end": "13:00"}]})
        reading = make_reading(T0, soc_percent=1)
        assert len(evaluate(in_ist, RuleState(), reading, T0)[1]) == 1
        assert evaluate(in_utc_only, RuleState(), reading, T0)[1] == []

    def test_firing_starts_when_the_window_opens_and_ends_when_it_closes(self):
        # 10-min steps from 16:50 IST. Window 17:00-18:00. Open at idx1, closes at idx7 (18:00 exclusive).
        rule = mk(schedule={"windows": [{"start": "17:00", "end": "18:00"}]})
        series = make_series(T0 - timedelta(minutes=40), 600, soc_percent=[15] * 10)
        fired, states = replay(rule, series)
        assert fired == [1]
        assert states[6].phase == "firing" and states[7].phase == "inactive"

    def test_time_hour_local_uses_the_homes_clock(self):
        rule = mk(trigger=cmp("time.hour_local", ">=", 22), actions=[NOTIFY])
        assert evaluate(rule, RuleState(), make_reading(T0), T0)[1] == []  # 17:30 local
        late = datetime(2026, 11, 9, 17, 0, tzinfo=UTC)  # 22:30 local
        assert len(evaluate(rule, RuleState(), make_reading(late), late)[1]) == 1


class TestCombinators:
    def test_all(self):
        rule = mk(trigger=all_of(SOC_LOW, cmp("load.power_kw", ">", 3)), actions=[NOTIFY])
        series = make_series(T0, 60, soc_percent=[15, 15, 50], load_kw=[1, 5, 5])
        assert replay(rule, series)[0] == [1]

    def test_any(self):
        rule = mk(trigger=any_of(SOC_LOW, cmp("load.power_kw", ">", 3)), actions=[NOTIFY])
        series = make_series(T0, 60, soc_percent=[50, 50, 15], load_kw=[1, 5, 1])
        assert replay(rule, series)[0] == [1]  # idx2: load is back to 1 but SOC took over, so no clear and no new edge

    def test_not(self):
        rule = mk(trigger=not_of(cmp("grid.available", "==", 1)), actions=[NOTIFY])
        series = make_series(T0, 60, grid_available=[True, False, False])
        assert replay(rule, series)[0] == [1]

    def test_nested(self):
        trigger = all_of(SOC_LOW, any_of(cmp("grid.available", "==", 0), cmp("solar.power_kw", "<", 0.1)))
        rule = mk(trigger=trigger)
        series = make_series(T0, 60, soc_percent=[15, 15, 15], grid_available=[True, True, False], solar_kw=[3, 0.0, 0.0])
        assert replay(rule, series)[0] == [1]


class TestDailyCap:
    def test_extra_firings_are_swallowed_then_reset_next_local_day(self):
        rule = mk(max_fires_per_day=2)
        series = soc_series([15, 50] * 5, step=120)  # would fire at idx 0,2,4,6,8
        fired, states = replay(rule, series)
        assert fired == [0, 2]
        assert states[-1].suppressed_total == 3
        next_day = make_reading(T0 + timedelta(days=1), soc_percent=15)
        state, again = evaluate(rule, states[-1], next_day, next_day.timestamp)
        assert len(again) == 1 and state.fires_today == 1

    def test_day_boundary_is_the_homes_local_midnight(self):
        # 18:29 UTC = 23:59 IST (same IST day), 18:31 UTC = 00:01 IST (next day).
        rule = mk(max_fires_per_day=1)
        t1 = datetime(2026, 11, 9, 18, 29, tzinfo=UTC)
        t2 = datetime(2026, 11, 9, 18, 31, tzinfo=UTC)
        series = [make_reading(t1, soc_percent=15), make_reading(t1 + timedelta(seconds=30), soc_percent=50),
                  make_reading(t2, soc_percent=15)]
        assert replay(rule, series)[0] == [0, 2]


class TestEvaluateAll:
    def test_all_rules_in_order_with_fresh_state_for_new_rules(self):
        low = mk(id="low")
        outage = mk(id="outage", trigger=cmp("grid.available", "==", 0), actions=[NOTIFY])
        quiet = mk(id="quiet", trigger=cmp("load.power_kw", ">", 40), actions=[NOTIFY])
        reading = make_reading(T0, soc_percent=10, grid_available=False, load_kw=1)
        states, fired = evaluate_all([low, outage, quiet], {"removed": RuleState(phase="firing", fired_at=1.0)}, reading, T0)
        assert [f.rule_id for f in fired] == ["low", "outage"]
        assert set(states) == {"low", "outage", "quiet"}  # the state of a rule that no longer exists is dropped
        again, fired2 = evaluate_all([low, outage, quiet], states, reading, T0 + timedelta(seconds=60))
        assert fired2 == [] and again["low"].phase == "firing"


class TestStateAndDeterminism:
    def test_state_is_json_dumpable_and_round_trips(self):
        _, states = replay(mk(for_seconds=120, cooldown_seconds=300), soc_series([15, 15, 15, 50, 15]))
        for state in states:
            blob = json.dumps(state.to_dict())
            assert RuleState.from_dict(json.loads(blob)) == state

    @pytest.mark.parametrize(
        "bad",
        [
            {"phase": "sleeping"},
            {"phase": "pending"},
            {"phase": "firing"},
            {"phase": "cooling", "fired_at": 1.0},
            {"unknown_key": 1},
            {"version": 99},
        ],
    )
    def test_corrupt_state_rejected_on_load(self, bad):
        with pytest.raises(ValueError):
            RuleState.from_dict(bad)
        with pytest.raises(ValueError):
            RuleState.from_dict("nope")  # type: ignore[arg-type]

    def test_evaluate_is_deterministic_and_does_not_mutate_inputs(self):
        rule, reading = mk(for_seconds=60), make_reading(T0, soc_percent=15)
        state = RuleState()
        first = evaluate(rule, state, reading, T0)
        second = evaluate(rule, state, reading, T0)
        assert first == second and state == RuleState()

    def test_replay_is_reproducible(self):
        series = soc_series([15, 50, 15, 15, 50, 15] * 5)
        assert replay(mk(), series)[0] == replay(mk(), series)[0]


# =====================================================================================================================
# Property-style: random readings never raise, cooldown and daily cap are never violated
# =====================================================================================================================


def _random_rules(rng: random.Random) -> list[Rule]:
    triggers = [
        SOC_LOW,
        cmp("grid.import_kw", ">", 1.0),
        all_of(cmp("solar.power_kw", ">", 2), cmp("load.power_kw", "<", 3)),
        any_of(cmp("grid.available", "==", 0), cmp("battery.soc_percent", "<=", 10)),
        not_of(cmp("battery.soc_percent", ">=", 30)),
        cmp("time.hour_local", ">=", 20),
    ]
    rules = []
    for index, trigger in enumerate(triggers):
        rules.append(Rule.model_validate(raw(
            id=f"p{index}", trigger=trigger, actions=[NOTIFY],
            for_seconds=rng.choice([0, 0, 60, 300, 900]),
            cooldown_seconds=rng.choice([60, 120, 600, 1800]),
            hysteresis=0.0 if index == 5 else rng.choice([0.0, 0.0, 0.1, 0.3]),  # the clock has no band
            max_fires_per_day=rng.choice([1, 3, 10]),
            schedule=rng.choice([None, None, {"windows": [{"start": "22:00", "end": "06:00"}]}]),
        )))
    return rules


def _random_reading(rng: random.Random, at: datetime) -> Reading:
    def pick(lo, hi):
        roll = rng.random()
        if roll < 0.12:
            return None
        if roll < 0.15:
            return rng.choice([float("nan"), float("inf"), -1e18, 1e18])
        return rng.uniform(lo, hi)

    return Reading(
        timestamp=at, soc_percent=pick(0, 100), solar_kw=pick(0, 6), load_kw=pick(0, 8), grid_import_kw=pick(0, 4),
        grid_available=rng.choice([True, True, False, None]),
    )


class TestProperties:
    def test_random_streams_never_raise_and_respect_cooldown_and_daily_cap(self):
        rng = random.Random(20261109)
        for _ in range(30):
            rules = _random_rules(rng)
            states: dict[str, RuleState] = {}
            fired_at: dict[str, list[datetime]] = {r.id: [] for r in rules}
            clock = T0 + timedelta(hours=rng.randint(0, 23))
            for _ in range(90):
                clock += timedelta(seconds=rng.choice([1, 30, 60, 60, 120, 600, 900, 4000]))
                reading = _random_reading(rng, clock - timedelta(seconds=rng.choice([0, 0, 0, 700])))
                states, firings = evaluate_all(rules, states, reading, clock)
                for firing in firings:
                    fired_at[firing.rule_id].append(firing.fired_at)
                for state in states.values():
                    assert RuleState.from_dict(json.loads(json.dumps(state.to_dict()))) == state
            for rule in rules:
                times = fired_at[rule.id]
                for earlier, later in zip(times, times[1:]):
                    assert (later - earlier).total_seconds() >= rule.cooldown_seconds, rule.id
                per_day: dict[str, int] = {}
                for ts in times:
                    day = ts.astimezone(timezone(timedelta(hours=5, minutes=30))).date().isoformat()
                    per_day[day] = per_day.get(day, 0) + 1
                assert max(per_day.values(), default=0) <= rule.max_fires_per_day, rule.id

    def test_random_valid_rule_json_round_trips(self):
        rng = random.Random(7)
        for rule in _random_rules(rng) * 3:
            assert validate_rule(json.loads(rule.model_dump_json()), CTX) == rule

    def test_garbage_input_never_raises_anything_but_validation_error(self):
        rng = random.Random(3)
        junk = [None, 1, "x", [], {}, {"trigger": 5}, {"trigger": {"kind": "all", "conditions": "x"}},
                {"id": "a", "name": "n", "trigger": {"kind": "not"}, "actions": "x"}]
        for _ in range(40):
            candidate = copy.deepcopy(FULL)
            key = rng.choice(list(candidate))
            candidate[key] = rng.choice(junk)
            try:
                validate_rule(candidate, CTX)
            except RuleValidationError:
                pass
        for item in junk:
            with pytest.raises(RuleValidationError):
                validate_rule(item, CTX)  # type: ignore[arg-type]


# =====================================================================================================================
# Dry run
# =====================================================================================================================


class TestDryRun:
    def test_instant_rule_matches_hand_computed_firings(self):
        # step 300 s, cooldown 60: [50,50,15,15,15,50,50,15,50]: fire idx2 (600 s) and idx7 (2100 s).
        series = soc_series([50, 50, 15, 15, 15, 50, 50, 15, 50], step=300)
        report = dry_run(mk(), series)
        assert report.fired_at == [T0 + timedelta(seconds=600), T0 + timedelta(seconds=2100)]
        summary = report.summary()
        assert summary["fired_count"] == 2
        assert summary["first_fired_at"] == (T0 + timedelta(seconds=600)).isoformat()
        assert summary["per_day_max"] == 2 and summary["noisy"] is False
        assert summary["longest_pending_seconds"] == 0
        assert summary["min_gap_between_fires_seconds"] == 1500
        assert summary["readings_used"] == 9

    def test_for_seconds_rule_and_longest_pending(self):
        # step 300, for 600: pending idx1 (300 s), fires idx3 (900 s); pending idx5 (1500 s) dropped idx6;
        # pending idx7 (2100 s), fires idx9 (2700 s). Longest pending = 600 s.
        series = soc_series([50, 15, 15, 15, 50, 15, 50, 15, 15, 15], step=300)
        report = dry_run(mk(for_seconds=600), series)
        assert [(f - T0).total_seconds() for f in report.fired_at] == [900, 2700]
        assert report.longest_pending_seconds == 600

    def test_longest_pending_counts_an_aborted_timer(self):
        # pending from 60 s, true again at 120 s and 180 s, dropped at 240 s: held for 120 s although it never fired.
        series = soc_series([50, 15, 15, 15, 50], step=60)
        report = dry_run(mk(for_seconds=600), series)
        assert report.fired_at == [] and report.longest_pending_seconds == 120

    def test_noisy_rule_is_flagged_and_counted_per_local_day(self):
        # alternating every 120 s from 23:30 IST: 10 firings; idx0..: t=0 -> 23:30 IST day 1, ..., crosses local midnight.
        start = datetime(2026, 11, 9, 18, 0, tzinfo=UTC)  # 23:30 IST
        series = soc_series([15, 50] * 15, step=120, start=start)  # 15 firings, at 0,240,...? no: every 240 s
        report = dry_run(mk(max_fires_per_day=50), series)
        assert len(report.fired_at) == 15
        # day 1 IST holds the firings before 00:00 IST (t < 1800 s): fire offsets 0,240,...,1680 -> 8 firings
        assert report.per_day == {"2026-11-09": 8, "2026-11-10": 7}
        assert report.per_day_max == 8 and report.noisy
        assert report.summary()["noisy_threshold_per_day"] == 3
        assert report.min_gap_seconds == 240

    def test_noisy_threshold_is_configurable(self):
        series = soc_series([15, 50] * 3, step=120)
        assert dry_run(mk(), series).noisy is False  # 3 firings, threshold 3
        assert dry_run(mk(), series, noisy_threshold_per_day=2).noisy is True

    def test_daily_cap_is_visible_in_the_report(self):
        series = soc_series([15, 50] * 5, step=120)
        report = dry_run(mk(max_fires_per_day=2), series)
        assert len(report.fired_at) == 2 and report.suppressed_count == 3

    def test_unsorted_history_is_sorted_and_disabled_rules_are_replayed(self):
        series = soc_series([50, 15, 15, 50, 15], step=300)
        forward = dry_run(mk(), series)
        backward = dry_run(mk(enabled=False), list(reversed(series)))
        assert forward.fired_at == backward.fired_at and len(forward.fired_at) == 2

    def test_undecidable_readings_are_counted(self):
        series = soc_series([15, None, None, 50], step=60)
        report = dry_run(mk(), series)
        assert report.unknown_readings == 2 and len(report.fired_at) == 1

    def test_sparse_history_widens_the_stale_limit(self):
        # 15-minute history with for_seconds 1800: the default 600 s limit would make this unfireable.
        report = dry_run(mk(for_seconds=1800), soc_series([15] * 6, step=900))
        assert report.stale_limit_seconds == 1800 and len(report.fired_at) == 1

    def test_empty_history(self):
        report = dry_run(mk(), [])
        assert report.summary()["fired_count"] == 0 and report.summary()["first_fired_at"] is None
        assert report.per_day_max == 0 and not report.noisy

    def test_summary_is_json_serialisable(self):
        json.dumps(dry_run(mk(), soc_series([15, 50, 15], step=300)).summary())


# =====================================================================================================================
# Adapters, templates, mapping, built-in alerts
# =====================================================================================================================


def telemetry_dict(**over) -> dict:
    base = dict(
        timestamp="2026-11-09T12:00:00Z", household_id="home-1", data_source="sim", data_quality="ok",
        solar_power_kw=2.0, solar_energy_interval_kwh=0.1, solar_energy_today_kwh=5, solar_energy_total_kwh=100,
        home_load_power_kw=1.5, home_consumption_interval_kwh=0.1, home_consumption_today_kwh=5,
        home_consumption_total_kwh=100, battery_soc_percent=42.0, battery_soh_percent=98.0,
        battery_charge_power_kw=0.5, battery_discharge_power_kw=0.0, battery_energy_available_kwh=5,
        battery_status="charging", grid_status="connected", grid_import_power_kw=0.0, grid_export_power_kw=0.0,
        total_import_kwh=1, total_export_kwh=1, solar_to_home_kw=1.5, solar_to_battery_kw=0.5, solar_to_grid_kw=0.0,
        battery_to_home_kw=0.0, grid_to_home_kw=0.0, unserved_load_kw=0.0,
        devices=[dict(device_id="geyser", device_name="Geyser", device_type="heater", rated_power_kw=2.0,
                      current_state="on", current_power_kw=1.8, energy_interval_kwh=0.1, critical=False,
                      controllable=True)],
    )
    base.update(over)
    return base


class TestAdapter:
    def test_from_telemetry_record(self):
        from app.modules.energy.schemas import TelemetryRecord

        record = TelemetryRecord.model_validate(telemetry_dict())
        reading = reading_from_record(record, price_inr_per_kwh=7.5)
        assert reading.timestamp == datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
        assert (reading.soc_percent, reading.solar_kw, reading.load_kw) == (42.0, 2.0, 1.5)
        assert reading.battery_charge_kw == 0.5 and reading.grid_available is True
        assert reading.device_power_kw == {"geyser": 1.8}
        assert reading.price_import_inr_per_kwh == 7.5 and reading.battery_temperature_c is None

    def test_outage_statuses_and_unknown_status(self):
        for status in ("outage", "OFF", "disconnected", "islanded"):
            assert reading_from_record(telemetry_dict(grid_status=status)).grid_available is False
        assert reading_from_record({"timestamp": T0}).grid_available is None

    def test_missing_data_quality_blanks_telemetry(self):
        reading = reading_from_record(telemetry_dict(data_quality="missing"))
        assert reading.soc_percent is None and reading.solar_kw is None and reading.device_power_kw == {}
        assert reading_from_record(telemetry_dict(data_quality="degraded")).soc_percent == 42.0

    def test_extra_battery_temperature_is_picked_up(self):
        assert reading_from_record(telemetry_dict(battery_temperature_c=31.5)).battery_temperature_c == 31.5

    def test_record_without_timestamp_is_an_error(self):
        with pytest.raises(ValueError):
            reading_from_record({"battery_soc_percent": 3})

    def test_naive_timestamps_are_utc(self):
        assert make_reading(datetime(2026, 1, 1)).timestamp.tzinfo is not None

    def test_make_series_pads_short_columns_with_none(self):
        series = make_series(T0, 60, soc_percent=[1, 2, 3], solar_kw=[9])
        assert [r.solar_kw for r in series] == [9.0, None, None]
        assert series[2].timestamp == T0 + timedelta(seconds=120)


class TestTemplatesAndMapping:
    def test_every_template_renders_within_notification_limits(self):
        for spec in TEMPLATES.values():
            params = {p.name: p.max_value for p in spec.params}
            out = render_notification(spec.id, params, "N" * 80, "app")
            assert out.title and len(out.title) <= 160 and 0 < len(out.body) <= 500

    def test_rule_name_is_data_not_a_format_string(self):
        out = render_notification("rule_triggered", {}, "{threshold_percent} {0}", "app")
        assert "{threshold_percent} {0}" in out.body

    def test_priorities_come_from_alerts_module(self):
        rule = mk(actions=[{"kind": "notify", "channel": "push", "template": "battery_low",
                            "params": {"threshold_percent": 20}}])
        out = notification_for(rule.actions[0], rule)
        assert (out.kind, out.priority, out.channel) == ("low_battery", "critical", "push")
        assert "Low battery" in out.body and "20%" in out.body

    def test_proposed_command_mapping(self):
        rule = validate_rule(raw(actions=[
            {"kind": "propose_device_action", "action": "turn_off", "device": "geyser"},
            {"kind": "propose_device_action", "action": "set_level", "device": "ev_charger", "level_percent": 50,
             "duration_minutes": 30}]), CTX)
        off, level = (proposed_command(a) for a in rule.actions)
        assert (off.device, off.command) == ("geyser", "off")
        assert (level.command, level.level_percent, level.duration_minutes) == ("set_level", 50.0, 30)

    def test_legacy_automation_projection(self):
        device_rule = validate_rule(raw(enabled=False, actions=[
            {"kind": "propose_device_action", "action": "turn_on", "device": "geyser"}]), CTX)
        fields = legacy_automation_fields(device_rule)
        assert fields == {"name": "Low battery", "device": "geyser", "command": "on", "surplus_kw_min": None,
                          "soc_percent_min": None, "mode": "dsl_v1", "enabled": False}
        assert legacy_automation_fields(validate_rule(raw(), CTX)) is None  # notify-only: no legacy row
        level_only = validate_rule(raw(actions=[{"kind": "propose_device_action", "action": "set_level",
                                                 "device": "geyser", "level_percent": 40}]), CTX)
        assert legacy_automation_fields(level_only) is None


class TestBuiltinAlertsParity:
    """The engine's built-in rules must agree with `alerts_from_home` on the same readings."""

    def test_builtin_rules_validate(self):
        for rule in builtin_alert_rules():
            validate_rule(rule.model_dump(mode="json"), ValidationContext())

    @pytest.mark.parametrize("soc", [None, 0.0, 5.0, 15.0, 15.1, 40.0])
    @pytest.mark.parametrize("grid", [None, "connected", "outage", "off", "islanded", "Disconnected"])
    @pytest.mark.parametrize("import_kw", [None, 0.0, 1.0, 1.01, 3.0])
    def test_same_decisions_as_alerts_module(self, soc, grid, import_kw):
        record = {"timestamp": T0, "battery_soc_percent": soc, "grid_status": grid, "grid_import_power_kw": import_kw}
        reading = reading_from_record(record)
        expected = {a.kind for a in alerts_from_home(
            soc_percent=soc, reserve_percent=15.0, grid_status=grid, import_kw=import_kw, solar_kw=None,
            radiation_wm2=None, data_quality=None, fault_code=None)}
        rules = builtin_alert_rules(sustained_import_seconds=0)
        _, firings = evaluate_all(rules, {}, reading, T0)
        got = {{"builtin.low_battery": "low_battery", "builtin.grid_outage": "grid_outage",
                "builtin.high_grid_import": "high_grid_import"}[f.rule_id] for f in firings}
        assert got == expected & {"low_battery", "grid_outage", "high_grid_import"}

    def test_builtin_alerts_are_edge_triggered_unlike_alerts_from_home(self):
        series = soc_series([10] * 5)
        rules = builtin_alert_rules()
        states: dict = {}
        total = []
        for reading in series:
            states, firings = evaluate_all(rules, states, reading, reading.timestamp)
            total += [f.rule_id for f in firings]
        assert total == ["builtin.low_battery"]
