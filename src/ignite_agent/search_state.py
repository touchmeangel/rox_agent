from ignite_agent.tool_result import ToolResult

from dataclasses import dataclass, field
from typing import Callable

@dataclass
class SearchState:
    max_calls: int = field(
        default=6
    )
    used: int = 0
    _cache: dict[str, ToolResult] = field(default_factory=dict)

    def remaining(self) -> int:
        return max(0, self.max_calls - self.used)

    def consume(
        self, tool_name: str, args: str, perform: Callable[[], ToolResult]
    ) -> ToolResult:
        key = f"{tool_name}:{args.strip().lower()}"

        if key in self._cache:
            cached = self._cache[key]
            note = "\n\n(cached from earlier this step — no budget spent)"
            return ToolResult(
                cached.command,
                cached.stdout + (note if cached.success else ""),
                cached.stderr,
                cached.returncode,
            )

        if self.used >= self.max_calls:
            return ToolResult(
                f"{tool_name} {args}",
                "",
                f"Search budget exhausted ({self.max_calls} calls used this step). "
                "Proceed with what you've already found — do not call web_search "
                "or web_fetch again this step.",
                1,
            )

        self.used += 1
        result = perform()
        self._cache[key] = result

        if result.success:
            footer = f"\n\n({self.remaining()} of {self.max_calls} step searches remaining)"
            result = ToolResult(
                result.command, result.stdout + footer, result.stderr, result.returncode
            )
        return result