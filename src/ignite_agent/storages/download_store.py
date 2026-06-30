from pathlib import Path

from ignite_agent.storages.base import TempStorage


class DownloadStore(TempStorage):
    _PREFIX = "ignite_scratch_"

    def write_bytes(self, content: bytes, filename: str) -> Path:
        dest = self._ensure_dir() / filename
        dest.write_bytes(content)
        return dest
