"""
Make hidden tricks visible before the text is inspected.

* NFKC folds full-width and compatibility forms ("ｉｇｎｏｒｅ" becomes "ignore").
* Invisible and control characters (zero-width spaces, bidi overrides, Unicode "tag" characters used to
  smuggle text) are removed, and counted, because *having* them is itself a signal.
* Cyrillic and Greek look-alike letters inside a word that also contains Latin letters are folded to Latin
  ("ignоre" with a Cyrillic о). Words written entirely in another script are left alone, so Hindi and other
  languages are not touched.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_CONFUSABLES = {
    # Cyrillic
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "і": "i", "ѕ": "s", "ј": "j",
    "ԁ": "d", "һ": "h", "ԛ": "q", "ԝ": "w", "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H",
    "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X", "І": "I",
    # Greek
    "α": "a", "ο": "o", "ν": "v", "ρ": "p", "τ": "t", "ι": "i", "κ": "k", "Α": "A", "Β": "B", "Ε": "E",
    "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y",
    "Χ": "X",
}
_WORD = re.compile(r"\w+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")
_KEEP_CONTROL = frozenset("\t\n\r")


@dataclass(frozen=True)
class NormalizedText:
    text: str
    invisible_removed: int
    homoglyphs_folded: int


def _is_latin(char: str) -> bool:
    return char.isascii() and char.isalpha()


def _fold_word(match: re.Match[str]) -> str:
    word = match.group(0)
    if not any(_is_latin(c) for c in word):
        return word  # a word wholly in another script is not an attack
    return "".join(_CONFUSABLES.get(c, c) for c in word)


def normalize(text: str) -> NormalizedText:
    folded = unicodedata.normalize("NFKC", text)

    kept: list[str] = []
    removed = 0
    for char in folded:
        if char not in _KEEP_CONTROL and unicodedata.category(char) in ("Cc", "Cf"):
            removed += 1
            continue
        kept.append(char)
    cleaned = "".join(kept)

    mixed = _WORD.sub(_fold_word, cleaned)
    changed = sum(1 for a, b in zip(cleaned, mixed) if a != b)
    return NormalizedText(
        text=_WHITESPACE.sub(" ", mixed).strip(),
        invisible_removed=removed,
        homoglyphs_folded=changed,
    )
