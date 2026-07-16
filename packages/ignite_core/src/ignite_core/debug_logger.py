import json
import sys
import threading
import time
import gzip
import shutil
from pathlib import Path

from ignite_core.tool_result import ToolResult

_MAX_LOG_BYTES = 50 * 1024 * 1024 

def _rotate_if_needed(path: Path) -> None:
    if not path.exists() or path.stat().st_size < _MAX_LOG_BYTES:
        return
    rotated = path.with_suffix(path.suffix + f".{int(time.time())}.gz")
    with path.open("rb") as src, gzip.open(rotated, "wb") as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()

class DebugLogger:
    def __init__(self, target: str | None = None):
        self.target = target
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.target)

    def configure(self, target: str | None) -> None:
        self.target = target

    def _write(self, record: dict) -> None:
        if not self.enabled:
            return

        line = json.dumps(record, ensure_ascii=False, default=str)

        with self._lock:
            if self.target in ("stderr", "-"):
                print(line, file=sys.stderr, flush=True)
                return
            try:
                path = Path(self.target)
                path.parent.mkdir(parents=True, exist_ok=True)
                _rotate_if_needed(path)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as exc:
                print(f"[debug log write failed: {exc}]", file=sys.stderr)

    def log(self, tool_name: str, args: str, result: ToolResult) -> None:
        self._write(
            {
                "ts": time.time(),
                "category": "tool",
                "tool": tool_name,
                "args": args,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )

    def log_llm_call(
        self,
        *,
        step: str,
        provider: str,
        model: str,
        messages: list[dict],
        response: str,
        duration_s: float,
        message_offset: int = 0,
        error: str | None = None,
    ) -> None:
        self._write(
            {
                "ts": time.time(),
                "category": "llm_call",
                "step": step,
                "provider": provider,
                "model": model,
                "duration_s": round(duration_s, 3),
                "message_offset": message_offset,
                "messages": messages,
                "response": response,
                "error": error,
            }
        )

    def log_event(self, category: str, **fields) -> None:
        self._write({"ts": time.time(), "category": category, **fields})


default_debug_logger = DebugLogger()
