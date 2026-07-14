from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import TYPE_CHECKING

from ignite_core.tool_result import ToolResult

if TYPE_CHECKING:
    from ignite_core.rolling_window import RollingWindow
    from rich.console import Console


class Reporter(ABC):
    @abstractmethod
    def feed(self, text: str) -> None:
        pass

    @abstractmethod
    def stream_line(self, line: str) -> None:
        pass

    @abstractmethod
    def start_spinner(self, label: str) -> None: ...

    @abstractmethod
    def stop_spinner(self) -> None: ...

    @contextmanager
    def spinner(self, label: str):
        self.start_spinner(label)
        try:
            yield
        finally:
            self.stop_spinner()

    @abstractmethod
    def tool_started(self, tool_name: str, display: str) -> None: ...

    @abstractmethod
    def tool_finished(self, result: ToolResult, elapsed: float) -> None: ...

    @abstractmethod
    def thinking_start(self) -> None: ...

    @abstractmethod
    def thinking_delta(self, text: str) -> None: ...

    @abstractmethod
    def thinking_end(self, char_count: int) -> None: ...

    @abstractmethod
    def close_thinking_if_open(self) -> None: ...

    @abstractmethod
    def model_selected(self, model: str) -> None: ...

    @abstractmethod
    def model_fallback(
        self, from_model: str, to_model: str, error: Exception | None
    ) -> None: ...

    @abstractmethod
    def step_started(self, name: str, budget: int | None = None) -> None: ...

    @abstractmethod
    def step_finished(
        self,
        name: str,
        success: bool,
        iterations: int,
        plan_done: int | None = None,
        plan_total: int | None = None,
    ) -> None: ...

    @abstractmethod
    def run_summary(self, succeeded: int, failed: int) -> None: ...


class NullReporter(Reporter):
    def feed(self, text: str) -> None:
        pass

    def stream_line(self, line: str) -> None:
        pass

    def start_spinner(self, label: str) -> None:
        pass

    def stop_spinner(self) -> None:
        pass

    def tool_started(self, tool_name: str, display: str) -> None:
        pass

    def tool_finished(self, result: ToolResult, elapsed: float) -> None:
        pass

    def thinking_start(self) -> None:
        pass

    def thinking_delta(self, text: str) -> None:
        pass

    def thinking_end(self, char_count: int) -> None:
        pass

    def close_thinking_if_open(self) -> None:
        pass

    def model_selected(self, model: str) -> None:
        pass

    def model_fallback(
        self, from_model: str, to_model: str, error: Exception | None
    ) -> None:
        pass

    def step_started(self, name: str, budget: int | None = None) -> None:
        pass

    def step_finished(
        self,
        name: str,
        success: bool,
        iterations: int,
        plan_done: int | None = None,
        plan_total: int | None = None,
    ) -> None:
        pass

    def run_summary(self, succeeded: int, failed: int) -> None:
        pass


_RST = "\033[0m"
_DIM = "\033[2m"
_DIM_CYAN = "\033[2;36m"
_BOX_W = 52
_THINKING_LINE = f"{_DIM_CYAN}│{_RST}{_DIM} "
_THINKING_OPEN = f"\n{_DIM_CYAN}╭─ thinking {'─' * (_BOX_W - 13)}╮\n" + _THINKING_LINE
_STREAM_PREFIX = f"  {_DIM}"
_STREAM_SUFFIX = _RST


def _elapsed_str(seconds: float) -> str:
    if seconds < 0.1:
        return ""
    if seconds < 60:
        return f"  {_DIM}{seconds:.1f}s{_RST}"
    m, s = divmod(int(seconds), 60)
    return f"  {_DIM}{m}m{s:02d}s{_RST}"


def _format_tool_summary(result: ToolResult, elapsed: float) -> str:
    total = len(result.stdout) + len(result.stderr)
    size = f"{total / 1024:.1f} KB" if total >= 1024 else f"{total} B"
    time_ = _elapsed_str(elapsed)

    if result.success:
        if total == 0:
            return f"[green]✓[/green]{time_}"
        raw = result.stdout if result.stdout else result.stderr
        snippet = next((line.strip() for line in raw.splitlines() if line.strip()), "")
        if snippet and total < 400:
            safe_snippet = snippet[:72].replace("[", r"\[")
            snippet_str = f"  [dim]·  {safe_snippet}[/dim]"
        else:
            snippet_str = ""
        return f"[green]✓[/green]  {size}{time_}{snippet_str}"

    return f"[red]✗[/red]  exit {result.returncode}  {size}{time_}"


class RichReporter(Reporter):
    def __init__(self, window: RollingWindow, console: Console | None = None):
        self.window = window
        self.console = console or window.console
        self._thinking_chars = 0
        self._thinking_open = False

    def feed(self, text: str) -> None:
        self.window.feed(text)

    def stream_line(self, line: str) -> None:
        self.window.feed(f"{_STREAM_PREFIX}{line}{_STREAM_SUFFIX}\n")

    def start_spinner(self, label: str) -> None:
        self.window.start_spinner(label)

    def stop_spinner(self) -> None:
        self.window.stop_spinner()

    def tool_started(self, tool_name: str, display: str) -> None:
        self.window.feed(f"\n{_DIM_CYAN}$ {_RST}{display}\n")

    def tool_finished(self, result: ToolResult, elapsed: float) -> None:
        self.window.feed(f"  {_format_tool_summary(result, elapsed)}\n")

    def thinking_start(self) -> None:
        self._thinking_chars = 0
        self._thinking_open = True
        self.window.feed(_THINKING_OPEN)

    def thinking_delta(self, text: str) -> None:
        self._thinking_chars += len(text)
        self.window.feed(text.replace("\n", f"\n{_THINKING_LINE}"))

    def thinking_end(self, char_count: int) -> None:
        self._thinking_open = False
        tag = f" {char_count:,} chars " if char_count > 0 else ""
        dashes = max(2, _BOX_W - 2 - len(tag))
        self.window.feed(f"{_RST}\n{_DIM_CYAN}╰{'─' * dashes}{tag}╯{_RST}\n")

    def close_thinking_if_open(self) -> None:
        if self._thinking_open:
            self.thinking_end(self._thinking_chars)

    def model_selected(self, model: str) -> None:
        self.window.feed(f"{_DIM_CYAN}→ {_RST}{_DIM}{model}{_RST}\n")

    def model_fallback(
        self, from_model: str, to_model: str, error: Exception | None
    ) -> None:
        self.window.feed(
            f"†  [dim]{from_model} failed "
            f"({type(error).__name__}) → trying {to_model}[/dim]\n"
        )

    def step_started(self, name: str, budget: int | None = None) -> None:
        budget_str = f"  [dim]·  max {budget} iterations[/dim]" if budget is not None else ""
        self.console.print(f"\n[#1dcccc]●[/#1dcccc] [bold white]{name}[/bold white]{budget_str}")

    def step_finished(
        self,
        name: str,
        success: bool,
        iterations: int,
        plan_done: int | None = None,
        plan_total: int | None = None,
    ) -> None:
        icon = "[green]✓[/green]" if success else "[red]✗[/red]"
        plural = "" if iterations == 1 else "s"
        line = f"  [dim]⎿[/dim]  {icon}  [dim]{iterations} iteration{plural}[/dim]"
        if plan_total:
            line += f"  [dim]·  {plan_done}/{plan_total} planned steps done[/dim]"
        self.console.print(line)

    def run_summary(self, succeeded: int, failed: int) -> None:
        parts = []
        if succeeded:
            parts.append(f"[green]✓ {succeeded} succeeded[/green]")
        if failed:
            parts.append(f"[red]✗ {failed} failed[/red]")
        if not parts:
            parts.append("[dim]no steps ran[/dim]")
        self.console.print("\n" + "   ".join(parts))
