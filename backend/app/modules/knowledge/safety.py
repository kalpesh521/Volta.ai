"""
Ingest-side safety for documents (tracker 3.8, doc 04 §2 and §4).

Everything a document says reaches the answer model as a passage, so it is screened once, when it is uploaded, not
on every question:

* **Personal data is masked in the chunk** before it is embedded or stored, so neither the vector nor the text the
  model later reads holds a phone number, an email, a card number or a password. A value gets `[hidden <label>]`
  in place. Consumer and meter numbers are kept, because a bill question needs them and the policy already lets
  them be shown back; names and addresses are masked like any other personal data. The uploaded *file* is kept as
  the user gave it (it is theirs); only the searchable text is masked.
* **Instruction-like text is scored** with the same injection patterns the chat input uses. A chunk at or above the
  block score is **quarantined**: stored, counted and shown to the reviewer, but never retrieved. A chunk in the grey
  zone keeps its score and stays retrievable, and every passage is wrapped as untrusted data in the answer prompt
  (prompt v5 and later). A document made only of such chunks is refused.

Scoring is rules, not a model, so ingest stays deterministic and free.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.agents_platform.guardrails.injection import score_text
from app.agents_platform.guardrails.normalize import deleet, normalize
from app.agents_platform.guardrails.pii.detectors import POLICY, detect


@dataclass(frozen=True)
class ChunkScreen:
    text: str  # what is embedded and stored
    risk_score: float
    quarantined: bool
    masked_values: int


def mask_for_storage(text: str) -> tuple[str, int]:
    """Replace personal data with `[hidden <label>]`. Classes the policy lets a user see again are left alone."""
    found = [d for d in detect(text) if not POLICY[d.cls].show_in_answer]
    if not found:
        return text, 0
    parts: list[str] = []
    cursor = 0
    for detection in found:
        parts.append(text[cursor : detection.start])
        parts.append(f"[hidden {POLICY[detection.cls].label}]")
        cursor = detection.end
    parts.append(text[cursor:])
    return "".join(parts), len(found)


def risk_of(text: str) -> float:
    """The injection score of a chunk, read plainly and with leetspeak folded back."""
    normalized = normalize(text)
    plain = score_text(normalized.text, hidden_chars=normalized.invisible_removed).score
    folded_text, changed = deleet(normalized.text)
    if not changed:
        return plain
    return max(plain, score_text(folded_text, hidden_chars=normalized.invisible_removed).score)


def screen_chunk(text: str, *, quarantine_score: float) -> ChunkScreen:
    """Risk is judged on the original text (masking must not hide an attack), storage uses the masked text."""
    risk = round(risk_of(text), 3)
    masked, count = mask_for_storage(text)
    return ChunkScreen(text=masked, risk_score=risk, quarantined=risk >= quarantine_score, masked_values=count)
