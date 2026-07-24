import hashlib
from pathlib import Path

from rox_core.storages.base import TempStorage


class OverflowStore(TempStorage):
    _PREFIX = "rox_overflow_"

    def write(
        self, content: str, tool_name: str, suffix: str = ".txt"
    ) -> tuple[str, Path]:
        base = self._ensure_dir()
        key = hashlib.sha256(content.encode()).hexdigest()[:16]
        safe_tool = "".join(c if c.isalnum() else "_" for c in tool_name)[:40]
        path = base / f"{safe_tool}_{key}{suffix}"
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if existing != content:
                key = hashlib.sha256(content.encode()).hexdigest()
                path = base / f"{safe_tool}_{key}{suffix}"
                if not path.exists():
                    path.write_text(content, encoding="utf-8")
        else:
            path.write_text(content, encoding="utf-8")
        return key, path

    def read(self, key: str) -> Path | None:
        base = self._ensure_dir()
        matches = sorted(
            p for p in base.glob(f"*_{key}.*") if p.stem.endswith(f"_{key}")
        )
        return matches[0] if matches else None
