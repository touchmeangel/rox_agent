import os
import tempfile
from pathlib import Path

from ignite_agent.storages.base import TempStorage


class OverflowStore(TempStorage):
    _PREFIX = "ignite_overflow_"

    def write(self, content: str, prefix: str, suffix: str) -> Path:
        base = self._ensure_dir()
        fd, path_str = tempfile.mkstemp(suffix=suffix, prefix=f"{prefix}_", dir=base)
        path = Path(path_str)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        return path