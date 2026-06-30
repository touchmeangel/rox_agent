import os
import tempfile
from pathlib import Path

class OverflowStore:
    def __init__(self) -> None:
        self._dir: tempfile.TemporaryDirectory | None = None

    def _ensure_dir(self) -> Path:
        if self._dir is not None:
            return Path(self._dir.name)

        for base in (Path("/app/tmp"), None):  # None = OS default tempdir
            try:
                if base is not None:
                    base.mkdir(parents=True, exist_ok=True)
                self._dir = tempfile.TemporaryDirectory(
                    prefix="ignite_overflow_",
                    dir=str(base) if base is not None else None,
                )
                return Path(self._dir.name)
            except OSError:
                continue

        raise RuntimeError("No writable directory available for overflow logs")

    def write(self, content: str, prefix: str, suffix: str) -> Path:
        base = self._ensure_dir()
        fd, path_str = tempfile.mkstemp(suffix=suffix, prefix=f"{prefix}_", dir=base)
        path = Path(path_str)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def cleanup(self) -> None:
        if self._dir is not None:
            self._dir.cleanup()
            self._dir = None
