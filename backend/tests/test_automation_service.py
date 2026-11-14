"""
Automation rules end to end below the agent: store, approve, run, pause (tracker 4.1, 4.2).

The promise under test: a rule only runs if the user approved exactly that rule, it can only ever notify or suggest, and
nothing about it can move a device by itself.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.agents_platform.agents.device_action.models import ActionProposal, DeviceCommandOutbox
from app.agents_platform.automation import make_reading
from app.agents_platform.automation.context import device_names, validation_context
from app.agents_platform.automation.describe import describe_rule
from app.agents_platform.automation.executor import PAUSED_KIND, SUGGESTION_KIND, RuleExecutor
from app.agents_platform.automation.models import AutomationRuleDef
from app.agents_platform.automation.service import (
    AutomationInvalidError,
    AutomationLimitError,
    AutomationNotFoundError,
    AutomationService,
    AutomationStateError,
    FingerprintMismatchError,
)
from app.agents_platform.automation.dsl import Rule
from app.models.user import User
from app.modules.assistant.notifications.models import HomeNotification

T0 = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)
HOME, OTHER_HOME = "home_001", "home_002"

PROFILE = SimpleNamespace(
    battery_present=True,
    devices=[
        SimpleNamespace(device_id="dev_water_heater", device_name="Water heater", device_type="water_heater", critical=False, controllable=True),
        SimpleNamespace(device_id="dev_refrigerator", device_name="Refrigerator", device_type="refrigerator", critical=True, controllable=False),
    ],
)
CONTEXT = validation_context(PROFILE, timezone="Asia/Kolkata")


def low_battery(rule_id="low-battery", *, soc=20.0, cooldown=900) -> dict:
    return {
        "dsl_version": "1.0", "id": rule_id, "name": "Low battery heads-up", "timezone": "Asia/Kolkata",
        "trigger": {"kind": "compare", "metric": "battery.soc_percent", "op": "<=", "value": soc},
        "cooldown_seconds": cooldown, "max_fires_per_day": 10,
        "actions": [{"kind": "notify", "channel": "app", "template": "battery_low", "params": {"threshold_percent": soc}}],
    }


def heater_when_surplus(rule_id="heater-surplus") -> dict:
    return {
        "dsl_version": "1.0", "id": rule_id, "name": "Heater on surplus", "timezone": "Asia/Kolkata",
        "trigger": {"kind": "all", "conditions": [
            {"kind": "compare", "metric": "solar.power_kw", "op": ">=", "value": 3.0},
            {"kind": "compare", "metric": "battery.soc_percent", "op": ">=", "value": 60.0},
        ]},
        "cooldown_seconds": 1800, "max_fires_per_day": 3,
        "actions": [{"kind": "propose_device_action", "action": "turn_on", "device": "dev_water_heater", "duration_minutes": 30}],
    }


async def make_user(db) -> User:
    user = User(email=f"{uuid.uuid4().hex}@example.com", name="T", hashed_password=None)
    db.add(user)
    await db.flush()
    return user


def service(db, **kw) -> AutomationService:
    return AutomationService(db, max_rules=kw.pop("max_rules", 10), max_pending=kw.pop("max_pending", 5), clock=lambda: T0)


async def propose(db, user, raw=None, *, home=HOME, readings=(), svc=None):
    return await (svc or service(db)).propose(
        user_id=user.id, household_id=home, raw=raw or low_battery(), context=CONTEXT,
        readings=readings, device_names=device_names(PROFILE),
    )


async def approve(db, user, out, *, home=HOME):
    return await service(db).approve(user_id=user.id, household_id=home, automation_id=out.id, fingerprint=out.fingerprint)


async def notifications(db, kind=None) -> list[HomeNotification]:
    statement = select(HomeNotification).order_by(HomeNotification.created_at)
    if kind:
        statement = statement.where(HomeNotification.kind == kind)
    return list((await db.execute(statement)).scalars())


# --- proposing ------------------------------------------------------------------------------------


async def test_a_valid_rule_is_stored_pending_with_a_plain_summary_and_a_dry_run(db_session):
    user = await make_user(db_session)
    readings = [make_reading(T0 + timedelta(minutes=5 * i), soc_percent=50 - 5 * i) for i in range(10)]

    out = await propose(db_session, user, readings=readings)

    assert out.status == "pending_approval" and out.source == "api" and len(out.fingerprint) == 64
    assert "battery state of charge is at most 20 %" in out.summary and "notification" in out.summary
    assert out.dry_run["fired_count"] >= 1 and out.rule["id"] == "low-battery"


async def test_without_readings_the_dry_run_says_so_instead_of_inventing_a_result(db_session):
    out = await propose(db_session, await make_user(db_session))
    assert out.dry_run["readings_used"] == 0 and "no recent readings" in out.dry_run["note"]


async def test_an_invalid_rule_is_refused_with_every_problem_listed(db_session):
    bad = low_battery()
    bad["trigger"]["metric"] = "battery.mood"
    bad["actions"] = [{"kind": "propose_device_action", "action": "turn_off", "device": "dev_refrigerator"}]
    with pytest.raises(AutomationInvalidError) as excinfo:
        await propose(db_session, await make_user(db_session), bad)
    codes = {p["code"] for p in excinfo.value.details["problems"]}
    assert "unknown_metric" in codes and excinfo.value.status_code == 422
    assert await db_session.scalar(select(func.count()).select_from(AutomationRuleDef)) == 0


async def test_a_rule_can_never_switch_off_a_critical_device(db_session):
    bad = heater_when_surplus("fridge")
    bad["actions"] = [{"kind": "propose_device_action", "action": "turn_off", "device": "dev_refrigerator"}]
    with pytest.raises(AutomationInvalidError) as excinfo:
        await propose(db_session, await make_user(db_session), bad)
    assert any("critical" in p["code"] or "critical" in p["message"].lower() for p in excinfo.value.details["problems"])


async def test_a_second_rule_with_the_same_id_in_the_home_is_refused(db_session):
    user = await make_user(db_session)
    await propose(db_session, user)
    with pytest.raises(AutomationInvalidError):
        await propose(db_session, user)
    await propose(db_session, user, home=OTHER_HOME)  # the same id in another home is fine


async def test_a_home_cannot_be_filled_with_rules_or_proposals(db_session):
    user = await make_user(db_session)
    svc = service(db_session, max_rules=3, max_pending=2)
    await propose(db_session, user, low_battery("a"), svc=svc)
    await propose(db_session, user, low_battery("b"), svc=svc)
    with pytest.raises(AutomationLimitError, match="waiting for your approval"):
        await propose(db_session, user, low_battery("c"), svc=svc)
    first = (await svc.list(user_id=user.id, household_id=HOME))[0]
    await svc.approve(user_id=user.id, household_id=HOME, automation_id=first.id, fingerprint=first.fingerprint)
    await propose(db_session, user, low_battery("c"), svc=svc)
    with pytest.raises(AutomationLimitError, match="can hold 3"):
        await propose(db_session, user, low_battery("d"), svc=svc)


# --- approval -------------------------------------------------------------------------------------


async def test_approving_the_rule_that_was_shown_activates_it(db_session):
    user = await make_user(db_session)
    out = await propose(db_session, user)
    approved = await approve(db_session, user, out)
    assert approved.status == "active" and approved.approved_at.replace(tzinfo=timezone.utc) == T0


async def test_an_approval_for_a_different_fingerprint_is_refused(db_session):
    user = await make_user(db_session)
    out = await propose(db_session, user)
    with pytest.raises(FingerprintMismatchError):
        await service(db_session).approve(user_id=user.id, household_id=HOME, automation_id=out.id, fingerprint="0" * 64)
    assert (await service(db_session).get(user_id=user.id, household_id=HOME, automation_id=out.id)).status == "pending_approval"


async def test_a_rule_edited_after_it_was_shown_cannot_be_approved_even_with_the_old_fingerprint(db_session):
    user = await make_user(db_session)
    out = await propose(db_session, user)
    row = await db_session.get(AutomationRuleDef, out.id)
    edited = json.loads(row.rule_json)
    edited["trigger"]["value"] = 95.0  # now it would fire almost always
    row.rule_json = json.dumps(edited)
    await db_session.flush()

    with pytest.raises(FingerprintMismatchError):
        await approve(db_session, user, out)


async def test_only_a_pending_rule_can_be_approved_or_rejected(db_session):
    user = await make_user(db_session)
    out = await propose(db_session, user)
    await approve(db_session, user, out)
    with pytest.raises(AutomationStateError):
        await approve(db_session, user, out)
    with pytest.raises(AutomationStateError):
        await service(db_session).reject(user_id=user.id, household_id=HOME, automation_id=out.id)


async def test_pause_resume_reject_and_delete(db_session):
    user, svc = await make_user(db_session), service(db_session)
    out = await propose(db_session, user)
    assert (await svc.reject(user_id=user.id, household_id=HOME, automation_id=out.id)).status == "rejected"

    second = await propose(db_session, user, low_battery("second"))
    await approve(db_session, user, second)
    paused = await svc.pause(user_id=user.id, household_id=HOME, automation_id=second.id)
    assert paused.status == "paused" and paused.pause_reason == "paused by you"
    assert (await svc.resume(user_id=user.id, household_id=HOME, automation_id=second.id)).status == "active"
    with pytest.raises(AutomationStateError):
        await svc.resume(user_id=user.id, household_id=HOME, automation_id=second.id)
    await svc.delete(user_id=user.id, household_id=HOME, automation_id=second.id)
    with pytest.raises(AutomationNotFoundError):
        await svc.get(user_id=user.id, household_id=HOME, automation_id=second.id)


async def test_a_paused_rule_that_no_longer_matches_its_approval_stays_off(db_session):
    user, svc = await make_user(db_session), service(db_session)
    out = await propose(db_session, user)
    await approve(db_session, user, out)
    await svc.pause(user_id=user.id, household_id=HOME, automation_id=out.id)
    row = await db_session.get(AutomationRuleDef, out.id)
    row.rule_json = row.rule_json.replace('"value":20.0', '"value":99.0')
    await db_session.flush()
    with pytest.raises(FingerprintMismatchError):
        await svc.resume(user_id=user.id, household_id=HOME, automation_id=out.id)


async def test_another_users_or_another_homes_rule_is_a_404_for_every_operation(db_session):
    owner, stranger, svc = await make_user(db_session), await make_user(db_session), service(db_session)
    out = await propose(db_session, owner)
    for who, home in ((stranger, HOME), (owner, OTHER_HOME)):
        kw = dict(user_id=who.id, household_id=home, automation_id=out.id)
        for call in (svc.get(**kw), svc.approve(**kw, fingerprint=out.fingerprint), svc.reject(**kw), svc.pause(**kw), svc.resume(**kw), svc.delete(**kw)):
            with pytest.raises(AutomationNotFoundError):
                await call
    assert await svc.list(user_id=stranger.id, household_id=HOME) == []


# --- running --------------------------------------------------------------------------------------


def executor(**kw) -> RuleExecutor:
    return RuleExecutor(cache_seconds=kw.pop("cache_seconds", 0.0), device_names=lambda home: device_names(PROFILE), **kw)


async def active(db, user, raw=None, *, home=HOME):
    return await approve(db, user, await propose(db, user, raw or low_battery(), home=home), home=home)


async def test_a_rule_waiting_for_approval_never_runs(db_session):
    user = await make_user(db_session)
    await propose(db_session, user)
    result = await executor().process(db_session, HOME, make_reading(T0, soc_percent=5.0), T0)
    assert result.evaluated == 0 and await notifications(db_session) == []


async def test_an_approved_rule_fires_once_when_it_becomes_true_and_not_again_while_it_stays_true(db_session):
    user = await make_user(db_session)
    await active(db_session, user)
    ex = executor()

    first = await ex.process(db_session, HOME, make_reading(T0, soc_percent=18.0), T0)
    again = [await ex.process(db_session, HOME, make_reading(T0 + timedelta(seconds=30 * i), soc_percent=17.0), T0 + timedelta(seconds=30 * i)) for i in range(1, 6)]

    assert first.fired == 1 and first.notified == 1 and all(r.fired == 0 for r in again)
    sent = await notifications(db_session, "low_battery")
    assert len(sent) == 1 and "20%" in sent[0].body and "Low battery heads-up" in sent[0].body


async def test_the_firing_is_recorded_on_the_rule(db_session):
    user = await make_user(db_session)
    out = await active(db_session, user)
    await executor().process(db_session, HOME, make_reading(T0, soc_percent=10.0), T0)
    row = await db_session.get(AutomationRuleDef, out.id)
    fired_at = row.last_fired_at.replace(tzinfo=timezone.utc)  # SQLite hands back naive UTC
    assert row.fire_count == 1 and fired_at == T0 and json.loads(row.state_json)["phase"] != "inactive"


async def test_a_rule_that_stays_quiet_costs_no_writes(db_session):
    user = await make_user(db_session)
    out = await active(db_session, user)
    row = await db_session.get(AutomationRuleDef, out.id)
    before = row.state_json
    for i in range(5):
        await executor().process(db_session, HOME, make_reading(T0 + timedelta(seconds=i), soc_percent=80.0), T0 + timedelta(seconds=i))
    assert (await db_session.get(AutomationRuleDef, out.id)).state_json == before and await notifications(db_session) == []


async def test_a_suggestion_rule_notifies_and_creates_no_command_proposal_or_outbox_row(db_session):
    user = await make_user(db_session)
    await active(db_session, user, heater_when_surplus())

    result = await executor().process(db_session, HOME, make_reading(T0, solar_kw=3.5, soc_percent=70.0), T0)

    assert result.fired == 1 and result.suggested == 1 and result.notified == 0
    (note,) = await notifications(db_session, SUGGESTION_KIND)
    assert "turn on the water heater for 30 minutes" in note.body and "confirm" in note.body
    assert await db_session.scalar(select(func.count()).select_from(ActionProposal)) == 0, "a rule never proposes by itself"
    assert await db_session.scalar(select(func.count()).select_from(DeviceCommandOutbox)) == 0, "and never sends"


async def test_a_rule_whose_json_was_edited_after_approval_is_paused_and_never_fires(db_session):
    user = await make_user(db_session)
    out = await active(db_session, user)
    row = await db_session.get(AutomationRuleDef, out.id)
    row.rule_json = row.rule_json.replace('"value":20.0', '"value":99.0')
    await db_session.flush()

    result = await executor().process(db_session, HOME, make_reading(T0, soc_percent=50.0), T0)

    assert result.fired == 0 and (await db_session.get(AutomationRuleDef, out.id)).status == "paused"
    assert "no longer matches" in (await db_session.get(AutomationRuleDef, out.id)).pause_reason
    assert len(await notifications(db_session, PAUSED_KIND)) == 1 and await notifications(db_session, "low_battery") == []


async def test_an_unreadable_stored_rule_is_paused_not_a_crash(db_session):
    user = await make_user(db_session)
    out = await active(db_session, user)
    (await db_session.get(AutomationRuleDef, out.id)).rule_json = "{broken"
    await db_session.flush()
    result = await executor().process(db_session, HOME, make_reading(T0, soc_percent=5.0), T0)
    assert result.evaluated == 0 and (await db_session.get(AutomationRuleDef, out.id)).status == "paused"


async def test_state_survives_a_restart_so_a_rule_in_cooldown_does_not_fire_again(db_session):
    user = await make_user(db_session)
    await active(db_session, user, low_battery(cooldown=3600))
    await executor().process(db_session, HOME, make_reading(T0, soc_percent=10.0), T0)

    restarted = executor()  # a new process: no memory of the first
    later = T0 + timedelta(minutes=10)
    await restarted.process(db_session, HOME, make_reading(later - timedelta(seconds=30), soc_percent=60.0), later - timedelta(seconds=30))
    result = await restarted.process(db_session, HOME, make_reading(later, soc_percent=10.0), later)

    assert result.fired == 0 and len(await notifications(db_session, "low_battery")) == 1


async def test_corrupt_stored_state_resets_the_rule_instead_of_stopping_the_worker(db_session):
    user = await make_user(db_session)
    out = await active(db_session, user)
    (await db_session.get(AutomationRuleDef, out.id)).state_json = "not json at all"
    await db_session.flush()
    result = await executor().process(db_session, HOME, make_reading(T0, soc_percent=10.0), T0)
    assert result.fired == 1


async def test_an_approval_takes_effect_within_the_cache_window_and_at_once_after_forget(db_session):
    user = await make_user(db_session)
    ex = executor(cache_seconds=3600)
    assert (await ex.process(db_session, HOME, make_reading(T0, soc_percent=5.0), T0)).evaluated == 0
    out = await propose(db_session, user)
    await approve(db_session, user, out)

    assert (await ex.process(db_session, HOME, make_reading(T0, soc_percent=5.0), T0)).evaluated == 0, "cached 'no rules'"
    ex.forget(HOME)
    assert (await ex.process(db_session, HOME, make_reading(T0, soc_percent=5.0), T0)).evaluated == 1


async def test_rules_of_one_home_never_run_on_another_homes_readings(db_session):
    user = await make_user(db_session)
    await active(db_session, user, home=OTHER_HOME)
    result = await executor().process(db_session, HOME, make_reading(T0, soc_percent=5.0), T0)
    assert result.evaluated == 0 and await notifications(db_session) == []


async def test_two_firings_of_one_rule_are_both_notified_the_executor_does_not_swallow_them(db_session):
    user = await make_user(db_session)
    await active(db_session, user, low_battery(cooldown=60))
    ex = executor()
    await ex.process(db_session, HOME, make_reading(T0, soc_percent=10.0), T0)
    await ex.process(db_session, HOME, make_reading(T0 + timedelta(minutes=2), soc_percent=90.0), T0 + timedelta(minutes=2))
    await ex.process(db_session, HOME, make_reading(T0 + timedelta(minutes=4), soc_percent=10.0), T0 + timedelta(minutes=4))
    assert len(await notifications(db_session, "low_battery")) == 2


# --- description ----------------------------------------------------------------------------------


def test_the_description_is_generated_from_the_rule_and_names_the_device():
    rule = Rule.model_validate(heater_when_surplus())
    text = describe_rule(rule, device_names(PROFILE))
    assert "solar" in text.lower() and "at least 3 kW" in text and "water heater" in text and "30 minutes" in text
    assert "at most 3 times a day" in text and "ask you to confirm" in text


def test_a_boolean_state_reads_as_a_state_whichever_way_the_comparison_is_written():
    for op, value, expected in (("==", 0, "not available"), ("!=", 0, "is available"), ("<", 1, "not available"), (">", 0, "is available")):
        raw = {**low_battery("grid"), "trigger": {"kind": "compare", "metric": "grid.available", "op": op, "value": value},
               "actions": [{"kind": "notify", "channel": "app", "template": "grid_outage"}]}
        assert expected in describe_rule(Rule.model_validate(raw))
