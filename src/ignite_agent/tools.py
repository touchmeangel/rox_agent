from __future__ import annotations

import shlex
import subprocess
import threading
from pathlib import Path

from ignite_agent.rolling_window import RollingWindow


class ToolResult:
    def __init__(self, command: str, stdout: str, stderr: str, returncode: int):
        self.command = command
        self.stdout = stdout.strip()
        self.stderr = stderr.strip()
        self.returncode = returncode
        self.success = returncode == 0

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


def _stream_subprocess(
    args: list[str],
    cwd: str,
    timeout: int,
    window: RollingWindow,
) -> tuple[str, str, int]:
    """Run a subprocess, feeding stdout/stderr into `window` live as it arrives."""
    proc = subprocess.Popen(
        args,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    stdout_buf: list[str] = []
    stderr_buf: list[str] = []
    lock = threading.Lock()

    def _pump(pipe, sink: list[str]) -> None:
        try:
            for line in iter(pipe.readline, ""):
                sink.append(line)
                with lock:
                    window.feed(line)
        finally:
            pipe.close()

    threads = [
        threading.Thread(target=_pump, args=(proc.stdout, stdout_buf), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, stderr_buf), daemon=True),
    ]
    for t in threads:
        t.start()

    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        for t in threads:
            t.join(timeout=2)
        stderr_buf.append(f"\nTimed out after {timeout}s")
        return "".join(stdout_buf), "".join(stderr_buf), 1

    for t in threads:
        t.join()

    return "".join(stdout_buf), "".join(stderr_buf), returncode


def run_command(
    command: str,
    cwd: str,
    timeout: int = 180,
    window: RollingWindow | None = None,
) -> ToolResult:
    try:
        args = shlex.split(command)
        if window is not None:
            stdout, stderr, returncode = _stream_subprocess(args, cwd, timeout, window)
            return ToolResult(command, stdout, stderr, returncode)

        result = subprocess.run(  # noqa: S603 were in controlled docker container
            args,
            shell=False,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return ToolResult(command, result.stdout, result.stderr, result.returncode)
    except subprocess.TimeoutExpired:
        return ToolResult(command, "", f"Timed out after {timeout}s", 1)
    except Exception as exc:
        return ToolResult(command, "", str(exc), 1)


def slither(
    project_path: str, args: str = "./", window: RollingWindow | None = None
) -> ToolResult:
    return run_command(f"slither {args}", cwd=project_path, timeout=240, window=window)


def forge(
    project_path: str, args: str = "build", window: RollingWindow | None = None
) -> ToolResult:
    return run_command(f"forge {args}", cwd=project_path, timeout=180, window=window)


def git(
    project_path: str,
    args: str = "log --oneline -10",
    window: RollingWindow | None = None,
) -> ToolResult:
    return run_command(f"git {args}", cwd=project_path, timeout=30, window=window)


def read_file(project_path: str, relative_path: str) -> ToolResult:
    full = Path(project_path) / relative_path
    if not full.exists():
        return ToolResult(
            f"cat {relative_path}", "", f"File not found: {relative_path}", 1
        )
    try:
        return ToolResult(
            f"cat {relative_path}", full.read_text(errors="replace"), "", 0
        )
    except Exception as exc:
        return ToolResult(f"cat {relative_path}", "", str(exc), 1)


def list_sol_files(project_path: str, pattern: str = "**/*.sol") -> ToolResult:
    path = Path(project_path)
    files = sorted(str(f.relative_to(path)) for f in path.glob(pattern))
    return ToolResult(
        "find . -name '*.sol'",
        "\n".join(files) if files else "(no .sol files found)",
        "",
        0,
    )


def shell(
    project_path: str, command: str, window: RollingWindow | None = None
) -> ToolResult:
    return run_command(command, cwd=project_path, timeout=120, window=window)


TOOL_DESCRIPTIONS: dict[str, str] = {
    "slither": "Run slither static analysis. args: extra flags e.g. './ --json -'",
    "forge": "Run forge. args: subcommand + flags e.g. 'build', 'coverage', 'test -vvv'",
    "git": "Run git. args: subcommand e.g. 'log --oneline -20'",
    "read_file": "Read a source file. args: path relative to project root e.g. 'src/Vault.sol'",
    "list_sol_files": "List all .sol files. args: optional glob default '**/*.sol'",
    "shell": "Run any shell command. args: full command string",
}


def _summary(result: ToolResult) -> str:
    total = len(result.stdout) + len(result.stderr)
    size = f"{total / 1024:.1f} KB" if total >= 1024 else f"{total} B"

    if result.success:
        if total == 0:
            return "[green]✓[/green]  (no output)"
        if total < 400:
            raw = result.stdout if result.stdout else result.stderr
            snippet = next((line.strip() for line in raw.splitlines() if line.strip()), "")
            if snippet:
                return f"[green]✓[/green]  {size}  [dim]·  {snippet[:72]}[/dim]"
        return f"[green]✓[/green]  {size}"

    return f"[red]✗[/red]  exit {result.returncode}  {size}"


def dispatch(
    tool_name: str,
    args: str,
    project_path: str,
    window: RollingWindow | None = None,
) -> ToolResult:
    owns_window = window is None
    if owns_window:
        window = RollingWindow()
        window.__enter__()

    try:
        window.feed(f"⎿  {tool_name}  {args}\n")

        match tool_name:
            case "slither":
                result = slither(project_path, args or "./", window=window)
            case "forge":
                result = forge(project_path, args or "build", window=window)
            case "git":
                result = git(project_path, args or "log --oneline -10", window=window)
            case "read_file":
                result = read_file(project_path, args)
            case "list_sol_files":
                result = list_sol_files(project_path, args or "**/*.sol")
            case "shell":
                result = shell(project_path, args, window=window)
            case _:
                result = ToolResult(
                    tool_name,
                    "",
                    f"Unknown tool '{tool_name}'. Available: {list(TOOL_DESCRIPTIONS)}",
                    1,
                )

        window.feed(f"\n   {_summary(result)}\n")
    finally:
        if owns_window:
            window.__exit__(None, None, None)

    return result
