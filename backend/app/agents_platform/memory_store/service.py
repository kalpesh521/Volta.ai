"""
Long-term memory service: the safety gate every item passes, plus list, edit, delete, export and the expiry sweep.

The gate (`screen`), in order, for text from a command, the API or an edit:

    1. shape       not blank, at most AI_MEMORY_ITEM_MAX_CHARS characters, one line
    2. normalise   NFKC, hidden characters removed (the same view the input guard scores)
    3. personal    block-class values (card, bank account, UPI id, secrets) REJECT the item. Everything else
       data        (email, phone, ids, names and addresses recognised by cue) is replaced by a token, and only
                   the masked text is stored
    4. injection   the input guard's injection score, at or above AI_GUARD_BLOCK_SCORE, rejects. So does
                   an explicit directive aimed at the assistant ("always answer in ...", "you must ...").
                   A stored instruction would be a persistent prompt injection, replayed on every future question
    5. dedupe key  sha256 of the normalised masked text

The gate always enforces. It ignores `AI_GUARD_MODE=off|observe` and `AI_PII_MODE=off|observe`: those switches are
for measuring the chat path, and a durable store must not become the way around them.

Ownership: every call carries user id and household id. A foreign or unknown item id is a plain 404.
Rejections never echo the rejected text, and the logs carry a fingerprint, never the text.
"""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents_platform.guardrails.injection import score_text
from app.agents_platform.guardrails.normalize import deleet, normalize
from app.agents_platform.guardrails.pii import masker
from app.agents_platform.guardrails.pii.detectors import POLICY
from app.agents_platform.memory_store.commands import extract_remember_command, infer_kind
from app.agents_platform.memory_store.models import MemoryKind, MemorySource, UserMemoryItem, utcnow
from app.agents_platform.memory_store.repository import MemoryRepository
from app.ai.config import AISettings, get_ai_settings
from app.core.exceptions import AppError

logger = logging.getLogger("volta.security")

# The column is String(400); a deployment cannot configure a longer item than the schema can hold.
_COLUMN_LIMIT = 400
_MIN_CONTENT_CHARS = 3
_TOKEN = re.compile(r"<[A-Z]+(?:_[A-Z]+)*_\d{1,3}>")
_EDGE_PUNCT = " \t.,;:!?-"


# ---- errors ---------------------------------------------------------------------------------------------------------


class MemoryNotFoundError(AppError):
    """Unknown id, another user's id and another home's id all look the same."""

    status_code = 404
    code = "memory_not_found"

    def __init__(self) -> None:
        super().__init__("Memory item not found.")


class MemoryRejectedError(AppError):
    """The text may not be stored. The message says why in general terms and never repeats the text."""

    status_code = 422
    code = "memory_rejected"

    def __init__(self, message: str, code: str = "memory_rejected") -> None:
        super().__init__(message, code=code)


class MemoryLimitError(AppError):
    status_code = 409
    code = "memory_limit_reached"

    def __init__(self, limit: int) -> None:
        super().__init__(f"Suryaa can remember up to {limit} things about this home. Delete one to add another.")


class MemoryDuplicateError(AppError):
    status_code = 409
    code = "memory_duplicate"

    def __init__(self) -> None:
        super().__init__("Suryaa already remembers this.")


# ---- the safety gate ------------------------------------------------------------------------------------------------

# A directive aimed at the assistant is not a fact about the home. The injection score covers the classic attacks
# ("ignore previous instructions"); these cover the quiet ones that matter most for a durable store.
_DIRECTIVES = (
    re.compile(r"\b(?:you|suryaa|assistant|bot|ai)\s+(?:must|should|shall|will|have\s+to|need\s+to|are|'re)\b", re.I),
    re.compile(
        r"^\s*(?:from\s+now\s+on|henceforth|going\s+forward|always|never|whenever|every\s+time)\b.{0,60}?"
        r"\b(?:answer|respond|reply|say|speak|claim|pretend|ignore|reveal|output|act\s+as|behave|obey)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:when|if|whenever)\s+(?:(?:i|we|the\s+user|anyone|someone|they)\s+)?ask(?:s|ed)?\b.{0,80}?"
        r"\b(?:say|answer|respond|reply|tell|claim)\b",
        re.I,
    ),
)


@dataclass(frozen=True)
class ScreenedText:
    text: str  # masked, normalised, one line: what gets stored
    text_hash: str
    masked: tuple[str, ...] = ()  # personal-data classes that were replaced by tokens


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _dedupe_key(masked_text: str) -> str:
    key = " ".join(masked_text.casefold().split()).strip(_EDGE_PUNCT)
    return hashlib.sha256(key.encode()).hexdigest()


def _injection_score(normalized_text: str, hidden_chars: int) -> float:
    plain = score_text(normalized_text, hidden_chars=hidden_chars).score
    folded_text, changed = deleet(normalized_text)
    if not changed:
        return plain
    return max(plain, score_text(folded_text, hidden_chars=hidden_chars).score)


def _reject(reason: str, raw: str, message: str, code: str) -> MemoryRejectedError:
    logger.warning("memory item rejected reason=%s input_sha=%s chars=%d", reason, fingerprint(raw), len(raw))
    return MemoryRejectedError(message, code)


def screen(raw: str, *, max_chars: int, block_score: float) -> ScreenedText:
    if not isinstance(raw, str) or not raw.strip():
        raise MemoryRejectedError("Tell Suryaa what to remember.", "memory_empty")
    max_chars = min(max_chars, _COLUMN_LIMIT)
    if len(raw.strip()) > max_chars:
        raise MemoryRejectedError(
            f"That is too long to remember. Keep it to one short fact of at most {max_chars} characters.",
            "memory_too_long",
        )

    normalized = normalize(raw)
    text = " ".join(normalized.text.split())

    masking = masker.mask(text)
    if masking.blocked:
        labels = sorted({POLICY[cls].label for cls in masking.blocked})
        raise _reject(
            "sensitive_data",
            raw,
            f"This looks like sensitive data ({', '.join(labels)}). Suryaa never stores payment, account or "
            "secret details, so it was not saved.",
            "memory_sensitive_data",
        )

    if _injection_score(text, normalized.invisible_removed) >= block_score or any(p.search(text) for p in _DIRECTIVES):
        raise _reject(
            "injection",
            raw,
            "This reads like an instruction to the assistant rather than a fact about your home, so it was not saved.",
            "memory_not_allowed",
        )

    masked_text = masking.text
    if len(masked_text) > max_chars:
        raise MemoryRejectedError(
            f"That is too long to remember. Keep it to one short fact of at most {max_chars} characters.",
            "memory_too_long",
        )
    if len(re.sub(r"\W", "", _TOKEN.sub("", masked_text))) < _MIN_CONTENT_CHARS:
        raise MemoryRejectedError("There is nothing to remember in that.", "memory_empty")

    return ScreenedText(
        text=masked_text,
        text_hash=_dedupe_key(masked_text),
        masked=tuple(sorted(masking.counts)),
    )


def _aware(value: datetime) -> datetime:
    """SQLite returns naive datetimes. Everything is UTC; a naive input is read as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# ---- results --------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SaveResult:
    item: UserMemoryItem
    created: bool  # False: the same fact was already there
    masked: tuple[str, ...] = ()


@dataclass(frozen=True)
class RememberOutcome:
    """What the chat does with a "remember ..." message. `reply` is fixed text: no model call is needed."""

    saved: bool
    reply: str
    item: UserMemoryItem | None = None
    duplicate: bool = False
    error_code: str | None = None
    masked: tuple[str, ...] = field(default_factory=tuple)


PAGE_HINT = "You can review or delete it any time on the \"What Suryaa remembers\" page."


# ---- service --------------------------------------------------------------------------------------------------------


class MemoryService:
    def __init__(self, db: AsyncSession, settings: AISettings | None = None) -> None:
        self._db = db
        self._repo = MemoryRepository(db)
        self._cfg = settings or get_ai_settings()

    @property
    def max_items(self) -> int:
        return self._cfg.AI_MEMORY_MAX_ITEMS

    @property
    def item_max_chars(self) -> int:
        return min(self._cfg.AI_MEMORY_ITEM_MAX_CHARS, _COLUMN_LIMIT)

    def _screen(self, raw: str) -> ScreenedText:
        return screen(raw, max_chars=self.item_max_chars, block_score=self._cfg.AI_GUARD_BLOCK_SCORE)

    @staticmethod
    def _check_expiry(expires_at: datetime | None, now: datetime) -> datetime | None:
        if expires_at is None:
            return None
        expires_at = _aware(expires_at)
        if expires_at <= now:
            raise MemoryRejectedError("The expiry time must be in the future.", "memory_invalid_expiry")
        return expires_at

    # -- reads --

    async def list_items(
        self, user_id: uuid.UUID, household_id: str, *, now: datetime | None = None
    ) -> list[UserMemoryItem]:
        return await self._repo.list_active(user_id, household_id, now=now)

    async def get(
        self, user_id: uuid.UUID, household_id: str, item_id: str, *, now: datetime | None = None
    ) -> UserMemoryItem:
        item = await self._repo.get(user_id, household_id, item_id, now=now)
        if item is None:
            raise MemoryNotFoundError()
        return item

    async def export(
        self, user_id: uuid.UUID, household_id: str, *, now: datetime | None = None
    ) -> list[UserMemoryItem]:
        return await self._repo.list_active(user_id, household_id, now=now)

    # -- writes --

    async def create(
        self,
        user_id: uuid.UUID,
        household_id: str,
        text: str,
        *,
        kind: MemoryKind | None = None,
        expires_at: datetime | None = None,
        source: MemorySource = MemorySource.API,
        now: datetime | None = None,
    ) -> SaveResult:
        now = now or utcnow()
        checked = self._screen(text)
        expiry = self._check_expiry(expires_at, now)

        existing = await self._repo.find_by_hash(user_id, household_id, checked.text_hash)
        if existing is not None:
            if existing.expires_at is None or _aware(existing.expires_at) > now:
                # Saying it again is not an error. It refreshes the item's recency, and applies a new expiry if given.
                existing.updated_at = now
                if expiry is not None:
                    existing.expires_at = expiry
                await self._repo.flush()
                return SaveResult(existing, created=False, masked=checked.masked)
            await self._repo.delete(existing)  # expired, not swept yet: it must not block the same fact

        if await self._repo.count_active(user_id, household_id, now=now) >= self.max_items:
            raise MemoryLimitError(self.max_items)

        item = UserMemoryItem(
            id=uuid.uuid4().hex,
            user_id=user_id,
            household_id=household_id,
            kind=(kind or infer_kind(checked.text)).value,
            text=checked.text,
            text_hash=checked.text_hash,
            source=source.value,
            created_at=now,
            updated_at=now,
            expires_at=expiry,
        )
        try:
            async with self._db.begin_nested():
                await self._repo.add(item)
        except IntegrityError:
            # A parallel save of the same fact won the unique key. Report that one.
            winner = await self._repo.find_by_hash(user_id, household_id, checked.text_hash)
            if winner is None:
                raise
            return SaveResult(winner, created=False, masked=checked.masked)
        logger.info("memory saved source=%s kind=%s chars=%d masked=%s", source.value, item.kind, len(item.text),
                    ",".join(checked.masked))
        return SaveResult(item, created=True, masked=checked.masked)

    async def update(
        self,
        user_id: uuid.UUID,
        household_id: str,
        item_id: str,
        *,
        text: str | None = None,
        kind: MemoryKind | None = None,
        expires_at: datetime | None = None,
        clear_expiry: bool = False,
        now: datetime | None = None,
    ) -> SaveResult:
        now = now or utcnow()
        item = await self.get(user_id, household_id, item_id, now=now)

        masked: tuple[str, ...] = ()
        if text is not None:
            checked = self._screen(text)
            masked = checked.masked
            if checked.text_hash != item.text_hash:
                clash = await self._repo.find_by_hash(user_id, household_id, checked.text_hash)
                if clash is not None and clash.id != item.id:
                    if clash.expires_at is None or _aware(clash.expires_at) > now:
                        raise MemoryDuplicateError()
                    await self._repo.delete(clash)
                item.text, item.text_hash = checked.text, checked.text_hash
        if kind is not None:
            item.kind = kind.value
        if clear_expiry:
            item.expires_at = None
        elif expires_at is not None:
            item.expires_at = self._check_expiry(expires_at, now)
        item.updated_at = now
        try:
            async with self._db.begin_nested():
                await self._repo.flush()
        except IntegrityError:
            raise MemoryDuplicateError() from None
        return SaveResult(item, created=False, masked=masked)

    async def delete(self, user_id: uuid.UUID, household_id: str, item_id: str) -> None:
        item = await self.get(user_id, household_id, item_id)
        await self._repo.delete(item)

    async def delete_all(self, user_id: uuid.UUID, household_id: str) -> int:
        return await self._repo.delete_all(user_id, household_id)

    async def erase_user(self, user_id: uuid.UUID) -> int:
        """Every item in every home. For account deletion and erasure requests."""
        return await self._repo.delete_all_homes(user_id)

    # -- chat command --

    async def handle_remember_command(
        self, user_id: uuid.UUID, household_id: str, question: str, *, now: datetime | None = None
    ) -> RememberOutcome | None:
        """
        None when `question` is not a remember command (carry on with the normal answer). Otherwise the outcome
        with a fixed reply. A refusal is an outcome too, not an exception: the chat answers it like any message.
        """
        body = extract_remember_command(question)
        if body is None:
            return None
        try:
            saved = await self.create(user_id, household_id, body, source=MemorySource.COMMAND, now=now)
        except (MemoryRejectedError, MemoryLimitError) as exc:
            return RememberOutcome(saved=False, reply=exc.message, error_code=exc.code)
        if saved.created:
            reply = f"Got it. I will remember: \"{saved.item.text}\". {PAGE_HINT}"
        else:
            reply = f"I already remember that: \"{saved.item.text}\". {PAGE_HINT}"
        return RememberOutcome(
            saved=saved.created, reply=reply, item=saved.item, duplicate=not saved.created, masked=saved.masked
        )


# ---- module-level helpers for the supervisor and the hourly sweep ---------------------------------------------------


async def mark_used(
    db: AsyncSession,
    ids: Sequence[str],
    *,
    user_id: uuid.UUID,
    household_id: str,
    now: datetime | None = None,
) -> int:
    """Record that these items were shown to the model. Scoped: an id from another user or home is ignored."""
    return await MemoryRepository(db).mark_used(user_id, household_id, list(ids), now=now or utcnow())


async def purge_expired(db: AsyncSession, now: datetime | None = None) -> int:
    """Delete items whose `expires_at` has passed. Hourly-sweep compatible; the caller commits."""
    return await MemoryRepository(db).delete_expired(now or utcnow())
