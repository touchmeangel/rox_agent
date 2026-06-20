from ignite_agent.tool_result import ToolResult
from pathlib import Path
import threading
import time
import json
import sys

class DebugLogger:
    def __init__(self, target: str | None = None):
        self.target = target
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.target)

    def configure(self, target: str | None) -> None:
        self.target = target

    def log(self, tool_name: str, args: str, result: ToolResult) -> None:
        if not self.enabled:
            return

        record = {
            "ts": time.time(),
            "tool": tool_name,
            "args": args,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        line = json.dumps(record, ensure_ascii=False)

        with self._lock:
            if self.target in ("stderr", "-"):
                print(line, file=sys.stderr, flush=True)
                return
            try:
                path = Path(self.target)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as exc:
                print(f"[debug log write failed: {exc}]", file=sys.stderr)