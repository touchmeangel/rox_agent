from __future__ import annotations

import json


class ToolResult:
    def __init__(self, command: str, stdout: str, stderr: str, returncode: int, is_truncation_system_prompt: bool = False):
        self.command = command
        self.stdout = stdout.strip()
        self.stderr = stderr.strip()
        self.is_truncation_system_prompt = is_truncation_system_prompt
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
