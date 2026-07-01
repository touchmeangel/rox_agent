import tempfile
from pathlib import Path


class TempStorage:
    _PREFIX: str = "ignite_"

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
                    prefix=self._PREFIX,
                    dir=str(base) if base is not None else None,
                )
                return Path(self._dir.name)
            except OSError:
                continue

        raise RuntimeError(
            f"No writable directory available for {self._PREFIX.strip('_')}"
        )

    def path(self) -> Path:
        return self._ensure_dir()

    def cleanup(self) -> None:
        if self._dir is not None:
            self._dir.cleanup()
            self._dir = None
