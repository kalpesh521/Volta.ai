"""Local file store for original uploads. Keys are relative and cannot escape the root."""
from __future__ import annotations

from pathlib import Path


class DocumentStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.is_file():
            path.unlink()
        parent = path.parent
        if parent != self.root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()

    def _path(self, key: str) -> Path:
        parts = Path(key).parts
        if not key or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("Invalid storage key")
        return self.root.joinpath(*parts)
