from pathlib import Path

from rox_core.storages.base import TempStorage


class DownloadStore(TempStorage):
    _PREFIX = "rox_scratch_"

    def write_bytes(self, content: bytes, filename: str) -> Path:
        dest = self._ensure_dir() / filename
        dest.write_bytes(content)
        return dest
