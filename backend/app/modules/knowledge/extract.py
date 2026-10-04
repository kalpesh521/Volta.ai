"""Pull plain text out of an upload, one page at a time so citations keep a page number."""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

_TEXT_SUFFIXES = frozenset({".txt", ".md", ".markdown"})
_PDF_SUFFIX = ".pdf"
ALLOWED_SUFFIXES = _TEXT_SUFFIXES | {_PDF_SUFFIX}


@dataclass(frozen=True)
class PageText:
    number: int
    text: str


def extract_pages(filename: str, data: bytes) -> list[PageText]:
    suffix = _suffix(filename)
    if suffix in _TEXT_SUFFIXES:
        text = data.decode("utf-8", errors="replace").strip()
        return [PageText(1, text)] if text else []
    if suffix == _PDF_SUFFIX:
        return _pdf_pages(data)
    raise ValueError(f"Unsupported file type '{suffix or 'unknown'}'. Upload PDF, TXT, or Markdown.")


def _suffix(filename: str) -> str:
    name = (filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    dot = name.rfind(".")
    return name[dot:] if dot >= 0 else ""


def _pdf_pages(data: bytes) -> list[PageText]:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    pages: list[PageText] = []
    for index, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append(PageText(index, text))
    return pages
