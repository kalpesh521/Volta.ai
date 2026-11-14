"""
Run approved automation rules on live readings: the Monitor (tracker 4.1, 4.4).

For each reading of a home: load its *active* rules, check each still hashes to what the user approved, evaluate them
(state transitions only, see `evaluator.py`) and carry out what fired. What "carry out" means is deliberately small:

* `notify` writes an in-app notification from a fixed template.
* `propose_device_action` writes a **suggestion** notification ("your rule suggests turning on the water heater; say so
  in the chat to confirm"). It does not create a command and does not touch the outbox. The user still asks, the safety
  checks still run on live telemetry at that moment, and the user still confirms. A rule can never move a device by
  itself, however it was written.

State is kept in memory between readings and written to the row only when it *changes kind* (inactive, pending, firing,
cooling) or a rule fires, so a rule that is quiet costs no database writes. After a restart the stored state is read
back; a corrupt value resets to inactive instead of stopping the worker.

Run one worker per home set: two workers evaluating the same home would both fire (see `worker.py`).
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents_platform.automation.describe import describe_action
from app.agents_platform.automation.dsl import NotifyAction, ProposeDeviceAction, Rule, rule_fingerprint
from app.agents_platform.automation.mapping import notification_for
from app.agents_platform.automation.models import AutomationRuleDef
from app.agents_platform.automation.readings import Reading
from app.agents_platform.automation.state import RuleState
from app.agents_platform.automation.evaluator import evaluate_all
from app.core.metrics import AUTOMATION_EVALUATIONS, AUTOMATION_FIRINGS
from app.modules.assistant.notifications.deliver import save_notification

logger = logging.getLogger("volta.automation")

SUGGESTION_KIND = "automation_suggestion"
PAUSED_KIND = "automation_paused"


@dataclass
class _Loaded:
    row_id: uuid.UUID
    user_id: uuid.UUID
    rule: Rule
    fingerprint: str
    state: RuleState


@dataclass
class _Cache:
    loaded_at: float
    rules: dict[str, _Loaded] = field(default_factory=dict)


@dataclass
class ExecutionResult:
    evaluated: int = 0
    fired: int = 0
    notified: int = 0
    suggested: int = 0
    paused: int = 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _state_from_row(row: AutomationRuleDef) -> RuleState:
    try:
        return RuleState.from_dict(json.loads(row.state_json or "{}"))
    except (ValueError, TypeError, KeyError):
        logger.warning("automation state of rule %s was unreadable and was reset", row.rule_id)
        return RuleState()


class RuleExecutor:
    def __init__(
        self,
        *,
        cache_seconds: float = 15.0,
        device_names: Callable[[str], Mapping[str, str]] = lambda household_id: {},
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cache_seconds = cache_seconds
        self._device_names = device_names
        self._monotonic = monotonic
        self._cache: dict[str, _Cache] = {}
        self._names_now: Mapping[str, str] | None = None

    def forget(self, household_id: str) -> None:
        """Drop a home's cached rules, so an approval or a pause takes effect on the next reading."""
        self._cache.pop(household_id, None)

    async def process(
        self,
        db: AsyncSession,
        household_id: str,
        reading: Reading,
        now: datetime | None = None,
        *,
        device_names: Mapping[str, str] | None = None,
    ) -> ExecutionResult:
        """Evaluate one reading of one home. `device_names` (id -> name) wording for suggestions, if the caller has it."""
        now = now or _utcnow()
        self._names_now = device_names
        loaded = await self._rules(db, household_id)
        result = ExecutionResult()
        if not loaded:
            AUTOMATION_EVALUATIONS.labels(outcome="no_rules").inc()
            return result

        rules = {key: item.rule for key, item in loaded.items()}
        states = {key: item.state for key, item in loaded.items()}
        new_states, firings = evaluate_all(list(rules.values()), states, reading, now)
        result.evaluated = len(rules)
        AUTOMATION_EVALUATIONS.labels(outcome="evaluated").inc()

        fired_ids = {f.rule_id for f in firings}
        for key, item in loaded.items():
            updated = new_states[key]
            changed = updated.phase != item.state.phase
            item.state = updated
            if changed or key in fired_ids:
                await self._persist(db, item, fired=key in fired_ids, now=now)

        for firing in firings:
            item = loaded[firing.rule_id]
            if firing.fingerprint != item.fingerprint:  # the engine ran something other than what was approved
                await self._pause(db, item, "the rule no longer matches what you approved", result)
                continue
            result.fired += 1
            await self._act(db, item, firing, result)
        return result

    # --- loading -----------------------------------------------------------------------------------------

    async def _rules(self, db: AsyncSession, household_id: str) -> dict[str, _Loaded]:
        cached = self._cache.get(household_id)
        if cached is not None and self._monotonic() - cached.loaded_at < self._cache_seconds:
            return cached.rules
        rows = (
            await db.execute(
                select(AutomationRuleDef).where(
                    AutomationRuleDef.household_id == household_id, AutomationRuleDef.status == "active"
                )
            )
        ).scalars().all()
        previous = cached.rules if cached else {}
        rules: dict[str, _Loaded] = {}
        for row in rows:
            try:
                rule = Rule.model_validate_json(row.rule_json)
            except ValueError:
                await self._pause_row(db, row, "the stored rule could not be read")
                continue
            if rule_fingerprint(rule) != row.fingerprint:
                await self._pause_row(db, row, "the rule no longer matches what you approved")
                continue
            known = previous.get(row.rule_id)
            state = known.state if known and known.fingerprint == row.fingerprint else _state_from_row(row)
            rules[row.rule_id] = _Loaded(row.id, row.user_id, rule, row.fingerprint, state)
        self._cache[household_id] = _Cache(self._monotonic(), rules)
        return rules

    async def _persist(self, db: AsyncSession, item: _Loaded, *, fired: bool, now: datetime) -> None:
        row = await db.get(AutomationRuleDef, item.row_id)
        if row is None:
            return
        row.state_json = json.dumps(item.state.to_dict())
        if fired:
            row.last_fired_at, row.fire_count = now, row.fire_count + 1
        await db.flush()

    # --- acting --------------------------------------------------------------------------------------------

    async def _act(self, db: AsyncSession, item: _Loaded, firing, result: ExecutionResult) -> None:
        household_id = (await db.get(AutomationRuleDef, item.row_id)).household_id  # type: ignore[union-attr]
        names = self._names_now or self._device_names(household_id)
        for action in firing.actions:
            if isinstance(action, NotifyAction):
                rendered = notification_for(action, item.rule)
                await save_notification(
                    db, user_id=item.user_id, household_id=household_id, kind=rendered.kind,
                    title=rendered.title, body=rendered.body, ref_id=str(item.row_id), dedupe="none",
                )
                AUTOMATION_FIRINGS.labels(action="notify").inc()
                result.notified += 1
            elif isinstance(action, ProposeDeviceAction):
                what = describe_action(action, names)
                await save_notification(
                    db, user_id=item.user_id, household_id=household_id, kind=SUGGESTION_KIND,
                    title="Suggested action",
                    body=f'Your automation "{item.rule.name}" suggests you {what.removeprefix("ask you to confirm: ")}. '
                    "Ask for it in the chat and you will be asked to confirm.",
                    ref_id=str(item.row_id), dedupe="none",
                )
                AUTOMATION_FIRINGS.labels(action="suggest").inc()
                result.suggested += 1
        logger.info("automation fired rule=%s fingerprint=%s observed=%s", item.rule.id, firing.fingerprint[:12], dict(firing.observed))

    async def _pause(self, db: AsyncSession, item: _Loaded, reason: str, result: ExecutionResult) -> None:
        row = await db.get(AutomationRuleDef, item.row_id)
        if row is not None:
            await self._pause_row(db, row, reason)
        result.paused += 1
        self.forget(row.household_id if row is not None else "")

    async def _pause_row(self, db: AsyncSession, row: AutomationRuleDef, reason: str) -> None:
        row.status, row.pause_reason = "paused", reason[:200]
        await db.flush()
        await save_notification(
            db, user_id=row.user_id, household_id=row.household_id, kind=PAUSED_KIND, title="Automation paused",
            body=f'"{row.name}" was paused: {reason}. Review it before turning it back on.', ref_id=str(row.id), dedupe="none",
        )
        AUTOMATION_FIRINGS.labels(action="paused").inc()
        logger.warning("automation paused rule=%s reason=%s", row.rule_id, reason)
