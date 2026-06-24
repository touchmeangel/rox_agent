from dataclasses import dataclass
from typing import Any


@dataclass
class ToolRawResult:
    tool: str
    stdout: str
    stderr: str
    exit_code: int


@dataclass
class ToolParsedResult:
    tool: str
    success: bool
    data: Any
    error: str | None = None


@dataclass
class ToolDisplayResult:
    tool: str
    summary: str
    truncated: bool = False


def safe_truncate(text: str, limit: int = 4000) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False

    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.3) :]

    return (
        head + "\n\n... [TRUNCATED] ...\n\n" + tail,
        True,
    )
