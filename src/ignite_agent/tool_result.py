from __future__ import annotations

import json


class ToolResult:
    def __init__(self, command: str, stdout: str, stderr: str, returncode: int):
        self.command = command
        self.stdout = stdout.strip()
        self.stderr = stderr.strip()
        self.returncode = returncode
        self.success = returncode == 0

    def json_data(self) -> dict | None:
        raw = self.stdout.lstrip()
        if not raw.startswith(("{", "[")):
            return None
        try:
            return json.loads(self.stdout)
        except (json.JSONDecodeError, ValueError):
            return None

    def __str__(self) -> str:
        parts = [f"$ {self.command}"]
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            label = "STDERR" if not self.success else "STDERR (warnings)"
            parts.append(f"{label}:\n{self.stderr}")
        if not self.stdout and not self.stderr:
            parts.append("(no output)")
        return "\n".join(parts)

    def truncated(self, max_chars: int = 12_000) -> str:
        full = str(self)
        if len(full) <= max_chars:
            return full
        half = max_chars // 2
        return (
            full[:half]
            + f"\n\n... [truncated — {len(full) - max_chars} chars omitted] ...\n\n"
            + full[-half:]
        )
