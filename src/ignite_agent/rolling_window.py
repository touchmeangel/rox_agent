from __future__ import annotations

from rich.containers import Lines
from rich.console import Console
from rich.live import Live
from rich.text import Text
import threading
import time

STREAM_STYLE = "dim"


class RollingWindow:
    def __init__(self, max_lines: int = 4):
        self.console = Console()
        self.max_lines = max_lines
        self.lines: list[str] = []
        self.cur = ""
        self.live = None
        
        self._spinning = False
        self._spinner_text = ""
        self._current_spinner_line = None
        self._spin_thread = None

    def start_spinner(self, text: str):
        self._spinning = True
        self._spinner_text = text
        self._spin_thread = threading.Thread(target=self._animate_spinner, daemon=True)
        self._spin_thread.start()

    def stop_spinner(self):
        self._spinning = False
        self._current_spinner_line = None
        
        if self.live:
            self.live.update(self._view(), refresh=True)
            
        if self._spin_thread:
            self._spin_thread.join()
            
    def _animate_spinner(self):
        frames = ["·", "*", "✷", "✸", "✹", "✺", "✹", "✸", "✷", "*"]
        i = 0
        while self._spinning:
            frame = frames[i % len(frames)]
            
            self._current_spinner_line = f"[#1dcccc]{frame}[/#1dcccc] {self._spinner_text}"
            
            if self.live:
                self.live.update(self._view(), refresh=True)
            
            i += 1
            time.sleep(0.1)

    def _view(self):
        rows = list(self.lines)
        if self.cur:
            rows.append(self.cur)
            
        if self._spinning and self._current_spinner_line:
            rows.append(self._current_spinner_line)

        rows = rows[-self.max_lines :]
        
        text = Text.from_markup("\n".join(rows))
        text.style = STREAM_STYLE
        text.no_wrap = False
        text.overflow = "fold"

        width = self.console.size.width
        wrapped = text.wrap(self.console, width)
        return Lines(list(wrapped)[-self.max_lines :])

    def __enter__(self):
        self.live = Live(
            self._view(),
            console=self.console,
            auto_refresh=False,
            transient=False,
            vertical_overflow="crop",
        )
        self.live.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.live.__exit__(exc_type, exc, tb)
        self.console.print()
        return False

    def feed(self, text: str) -> None:
        parts = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        self.cur += parts[0]
        for part in parts[1:]:
            self.lines.append(self.cur)
            self.cur = part
        if self.live:
            self.live.update(self._view(), refresh=True)