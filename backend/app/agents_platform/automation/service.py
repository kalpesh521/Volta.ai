"""
Automation rules: propose, approve, pause, delete (tracker 4.1, 4.2).

The lifecycle exists to keep one promise: **a rule only runs if the user approved exactly that rule.**

* A rule is *validated* (closed vocabulary, ranges, no critical device switched off, bounded repetition) and *dry-run*
  against recent readings before it is stored. It is stored `pending_approval`, and a pending rule never runs.
* Approving binds to the rule's fingerprint, which the user was shown. If the stored rule no longer hashes to it, the
  approval is refused. The executor checks the same fingerprint before every run.
* Every read and write is scoped to the owner and the home. Another user's rule, or another home's, is a 404.
* Limits: a home holds at most `AI_AUTOMATION_MAX_RULES` rules and `AI_AUTOMATION_MAX_PENDING` unapproved ones, so a
  chat cannot fill the table with proposals.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents_platform.automation.describe import describe_rule
from app.agents_platform.automation.dryrun import dry_run
from app.agents_platform.automation.dsl import Rule, rule_fingerprint
from app.agents_platform.automation.models import AutomationRuleDef
from app.agents_platform.automation.readings import Reading
from app.agents_platform.automation.schemas import AutomationOut
from app.agents_platform.automation.validation import RuleValidationError, ValidationContext, validate_rule
from app.core.exceptions import AppError

PENDING, ACTIVE, PAUSED, REJECTED = "pending_approval", "active", "paused", "rejected"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AutomationNotFoundError(AppError):
    status_code = 404
    code = "automation_not_found"

    def __init__(self) -> None:
        super().__init__("Automation not found.")


class AutomationLimitError(AppError):
    status_code = 409
    code = "automation_limit"


class AutomationStateError(AppError):
    status_code = 409
    code = "automation_state"


class AutomationInvalidError(AppError):
    """The rule failed validation. `details.problems` lists every problem so it can be fixed in one go."""

    status_code = 422
    code = "automation_invalid"

    def __init__(self, error: RuleValidationError) -> None:
        super().__init__("The rule is not valid.", details={"problems": error.to_dicts()})
        self.problems = error.problems


class FingerprintMismatchError(AutomationStateError):
    code = "automation_fingerprint_mismatch"

    def __init__(self) -> None:
        super().__init__("The rule changed since you were shown it. Review it again before approving.")


class AutomationService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        max_rules: int,
        max_pending: int,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._db = db
        self._max_rules = max_rules
        self._max_pending = max_pending
        self._clock = clock

    # --- create -----------------------------------------------------------------------------------

    async def existing_rule_ids(self, household_id: str) -> frozenset[str]:
        rows = await self._db.execute(select(AutomationRuleDef.rule_id).where(AutomationRuleDef.household_id == household_id))
        return frozenset(rows.scalars())

    async def propose(
        self,
        *,
        user_id: uuid.UUID,
        household_id: str,
        raw: Mapping[str, object],
        context: ValidationContext,
        readings: Sequence[Reading] = (),
        device_names: Mapping[str, str] | None = None,
        source: str = "api",
    ) -> AutomationOut:
        """Validate, dry-run and store a rule as pending. Raises `AutomationInvalidError` with every problem found."""
        await self._check_room(household_id)
        context = _with_existing_ids(context, await self.existing_rule_ids(household_id))
        try:
            rule = validate_rule(dict(raw), context)
        except RuleValidationError as error:
            raise AutomationInvalidError(error) from error
        report = dry_run(rule, readings).summary() if readings else {"readings_used": 0, "note": "no recent readings to replay"}
        row = AutomationRuleDef(
            user_id=user_id,
            household_id=household_id,
            rule_id=rule.id,
            name=rule.name,
            rule_json=rule.model_dump_json(),
            fingerprint=rule_fingerprint(rule),
            status=PENDING,
            source=source,
            dry_run_json=json.dumps(report, default=str),
        )
        self._db.add(row)
        await self._db.flush()
        return self._out(row, device_names)

    async def _check_room(self, household_id: str) -> None:
        total = await self._db.scalar(select(func.count()).select_from(AutomationRuleDef).where(AutomationRuleDef.household_id == household_id))
        pending = await self._db.scalar(
            select(func.count()).select_from(AutomationRuleDef).where(
                AutomationRuleDef.household_id == household_id, AutomationRuleDef.status == PENDING
            )
        )
        if (total or 0) >= self._max_rules:
            raise AutomationLimitError(f"A home can hold {self._max_rules} automations. Delete one first.")
        if (pending or 0) >= self._max_pending:
            raise AutomationLimitError(f"{self._max_pending} automations are already waiting for your approval. Approve or reject one first.")

    # --- lifecycle ----------------------------------------------------------------------------------

    async def approve(self, *, user_id: uuid.UUID, household_id: str, automation_id: uuid.UUID, fingerprint: str) -> AutomationOut:
        row = await self._owned(user_id, household_id, automation_id)
        if row.status != PENDING:
            raise AutomationStateError("Only an automation waiting for approval can be approved.")
        if fingerprint != row.fingerprint or rule_fingerprint(Rule.model_validate_json(row.rule_json)) != row.fingerprint:
            raise FingerprintMismatchError()
        row.status, row.approved_by, row.approved_at, row.pause_reason = ACTIVE, user_id, self._clock(), None
        await self._db.flush()
        return self._out(row)

    async def reject(self, *, user_id: uuid.UUID, household_id: str, automation_id: uuid.UUID) -> AutomationOut:
        row = await self._owned(user_id, household_id, automation_id)
        if row.status != PENDING:
            raise AutomationStateError("Only an automation waiting for approval can be rejected. Pause or delete the others.")
        row.status = REJECTED
        await self._db.flush()
        return self._out(row)

    async def pause(self, *, user_id: uuid.UUID, household_id: str, automation_id: uuid.UUID, reason: str = "paused by you") -> AutomationOut:
        row = await self._owned(user_id, household_id, automation_id)
        if row.status != ACTIVE:
            raise AutomationStateError("Only an active automation can be paused.")
        row.status, row.pause_reason = PAUSED, reason[:200]
        await self._db.flush()
        return self._out(row)

    async def resume(self, *, user_id: uuid.UUID, household_id: str, automation_id: uuid.UUID) -> AutomationOut:
        row = await self._owned(user_id, household_id, automation_id)
        if row.status != PAUSED:
            raise AutomationStateError("Only a paused automation can be resumed.")
        if rule_fingerprint(Rule.model_validate_json(row.rule_json)) != row.fingerprint:
            raise FingerprintMismatchError()  # a rule that no longer matches what was approved stays off
        row.status, row.pause_reason = ACTIVE, None
        await self._db.flush()
        return self._out(row)

    async def delete(self, *, user_id: uuid.UUID, household_id: str, automation_id: uuid.UUID) -> None:
        row = await self._owned(user_id, household_id, automation_id)
        await self._db.delete(row)
        await self._db.flush()

    async def get(self, *, user_id: uuid.UUID, household_id: str, automation_id: uuid.UUID, device_names: Mapping[str, str] | None = None) -> AutomationOut:
        return self._out(await self._owned(user_id, household_id, automation_id), device_names)

    async def list(self, *, user_id: uuid.UUID, household_id: str, device_names: Mapping[str, str] | None = None) -> list[AutomationOut]:
        rows = await self._db.execute(
            select(AutomationRuleDef)
            .where(AutomationRuleDef.user_id == user_id, AutomationRuleDef.household_id == household_id)
            .order_by(AutomationRuleDef.created_at.desc())
        )
        return [self._out(row, device_names) for row in rows.scalars()]

    # --- helpers --------------------------------------------------------------------------------------

    async def _owned(self, user_id: uuid.UUID, household_id: str, automation_id: uuid.UUID) -> AutomationRuleDef:
        row = (
            await self._db.execute(
                select(AutomationRuleDef).where(
                    AutomationRuleDef.id == automation_id,
                    AutomationRuleDef.user_id == user_id,
                    AutomationRuleDef.household_id == household_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:  # another user's, another home's and a missing one look identical
            raise AutomationNotFoundError()
        return row

    @staticmethod
    def _out(row: AutomationRuleDef, device_names: Mapping[str, str] | None = None) -> AutomationOut:
        rule = Rule.model_validate_json(row.rule_json)
        return AutomationOut(
            id=row.id,
            rule_id=row.rule_id,
            name=row.name,
            status=row.status,  # type: ignore[arg-type]
            source=row.source,  # type: ignore[arg-type]
            summary=describe_rule(rule, device_names),
            rule=json.loads(row.rule_json),
            fingerprint=row.fingerprint,
            dry_run=json.loads(row.dry_run_json or "{}"),
            pause_reason=row.pause_reason,
            approved_at=row.approved_at,
            last_fired_at=row.last_fired_at,
            fire_count=row.fire_count,
            created_at=row.created_at,
        )


def _with_existing_ids(context: ValidationContext, ids: frozenset[str]) -> ValidationContext:
    from dataclasses import replace

    return replace(context, existing_rule_ids=frozenset(context.existing_rule_ids) | ids)
