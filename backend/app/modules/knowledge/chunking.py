"""
Page-aware recursive chunking.

Each chunk stays on one page so a citation can name that page. Overlap is
copied onto the next chunk so a sentence split by the size limit is still
retrievable from either side.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.modules.knowledge.extract import PageText

_SEPARATORS = ("\n\n", "\n", ". ", " ")


@dataclass(frozen=True)
class TextChunk:
    index: int
    page_start: int
    page_end: int
    text: str
    content_hash: str


def embed_input(title: str, doc_type: str, page: int | None, text: str) -> str:
    """Contextual prefix. Stored text stays the raw chunk; only the vector sees this."""
    page_label = f"page {page}" if page else "page unknown"
    return f"{title}\n{doc_type}\n{page_label}\n{text}"


def chunk_pages(pages: list[PageText], *, size: int, overlap: int) -> list[TextChunk]:
    raw: list[tuple[int, str]] = []
    for page in pages:
        for piece in _split(page.text.strip(), size, _SEPARATORS):
            cleaned = piece.strip()
            if len(cleaned) < 40 and raw and raw[-1][0] == page.number:
                prev_page, prev_text = raw[-1]
                raw[-1] = (prev_page, f"{prev_text}\n{cleaned}".strip())
            elif cleaned:
                raw.append((page.number, cleaned))
    if overlap > 0:
        raw = _with_overlap(raw, overlap, size)
    chunks: list[TextChunk] = []
    for index, (page, text) in enumerate(raw):
        chunks.append(
            TextChunk(
                index=index,
                page_start=page,
                page_end=page,
                text=text,
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
        )
    return chunks


def _split(text: str, size: int, seps: tuple[str, ...]) -> list[str]:
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    sep = seps[0] if seps else ""
    rest = seps[1:] if seps else ()
    if not sep:
        return [text[i : i + size] for i in range(0, len(text), size)]
    parts = text.split(sep)
    chunks: list[str] = []
    buf = ""
    for part in parts:
        piece = part if not buf else f"{buf}{sep}{part}"
        if len(piece) <= size:
            buf = piece
            continue
        if buf:
            chunks.append(buf)
            buf = ""
        if len(part) <= size:
            buf = part
        else:
            chunks.extend(_split(part, size, rest))
    if buf:
        chunks.append(buf)
    return chunks


def _with_overlap(raw: list[tuple[int, str]], overlap: int, size: int) -> list[tuple[int, str]]:
    if len(raw) < 2:
        return raw
    out = [raw[0]]
    for page, text in raw[1:]:
        tail = out[-1][1][-overlap:]
        merged = text if text.startswith(tail) else f"{tail}{text}"
        out.append((page, merged[: size + overlap]))
    return out
