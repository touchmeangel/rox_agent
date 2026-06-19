from rich.live import Live
from rich.text import Text

STREAM_STYLE = "dim"

class RollingWindow:
    def __init__(self, max_lines: int = 4):
        from rich.console import Console

        self.console = Console()
        self.max_lines = max_lines
        self.lines: list[str] = []
        self.cur = ""
        self.live = None

    def _view(self):

        rows = (self.lines + ([self.cur] if self.cur else []))[-self.max_lines :]
        text = Text.from_markup("\n".join(rows))
        text.style = STREAM_STYLE
        text.no_wrap = True
        text.overflow = "ellipsis"
        return text

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