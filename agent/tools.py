from __future__ import annotations
import subprocess
from pathlib import Path


# ── Result type ───────────────────────────────────────────────────────────────


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
        """Return string representation, truncated with a clear marker."""
        full = str(self)
        if len(full) <= max_chars:
            return full
        half = max_chars // 2
        return (
            full[:half]
            + f"\n\n... [output truncated — {len(full) - max_chars} chars omitted] ...\n\n"
            + full[-half:]
        )


# ── Low-level runner ──────────────────────────────────────────────────────────


def run_command(command: str, cwd: str, timeout: int = 180) -> ToolResult:
    try:
        result = subprocess.run(
            command,
            shell=True,
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


# ── Named tools ───────────────────────────────────────────────────────────────


def slither(project_path: str, args: str = "./") -> ToolResult:
    """Run slither. Use --json - for machine-readable output."""
    return run_command(f"slither {args}", cwd=project_path, timeout=240)


def forge(project_path: str, args: str = "build") -> ToolResult:
    return run_command(f"forge {args}", cwd=project_path, timeout=180)


def git(project_path: str, args: str = "log --oneline -10") -> ToolResult:
    return run_command(f"git {args}", cwd=project_path, timeout=30)


def read_file(project_path: str, relative_path: str) -> ToolResult:
    full = Path(project_path) / relative_path
    if not full.exists():
        return ToolResult(
            f"cat {relative_path}", "", f"File not found: {relative_path}", 1
        )
    try:
        content = full.read_text(errors="replace")
        return ToolResult(f"cat {relative_path}", content, "", 0)
    except Exception as exc:
        return ToolResult(f"cat {relative_path}", "", str(exc), 1)


def list_sol_files(project_path: str, pattern: str = "**/*.sol") -> ToolResult:
    path = Path(project_path)
    files = sorted(str(f.relative_to(path)) for f in path.glob(pattern))
    return ToolResult(
        f"find . -name '*.sol'",
        "\n".join(files) if files else "(no .sol files found)",
        "",
        0,
    )


def shell(project_path: str, command: str) -> ToolResult:
    """Escape hatch: run any command the agent requests."""
    return run_command(command, cwd=project_path, timeout=120)


# ── Dispatcher ────────────────────────────────────────────────────────────────
# The agent produces {"tool": "<name>", "args": "..."} — this maps it to a fn.

TOOL_DESCRIPTIONS: dict[str, str] = {
    "slither": "Run slither static analysis. args: extra flags, e.g. './ --json -' or '--print call-graph ./'",
    "forge": "Run forge. args: subcommand + flags, e.g. 'build', 'coverage', 'test -vvv'",
    "git": "Run git. args: subcommand, e.g. 'log --oneline -20'",
    "read_file": "Read a source file. args: path relative to project root, e.g. 'src/Vault.sol'",
    "list_sol_files": "List all .sol files. args: optional glob, default '**/*.sol'",
    "shell": "Run any shell command. args: full command string",
}


def dispatch(tool_name: str, args: str, project_path: str) -> ToolResult:
    """Route a model tool-call to the right function."""
    match tool_name:
        case "slither":
            return slither(project_path, args or "./")
        case "forge":
            return forge(project_path, args or "build")
        case "git":
            return git(project_path, args or "log --oneline -10")
        case "read_file":
            return read_file(project_path, args)
        case "list_sol_files":
            return list_sol_files(project_path, args or "**/*.sol")
        case "shell":
            return shell(project_path, args)
        case _:
            return ToolResult(
                tool_name,
                "",
                f"Unknown tool '{tool_name}'. Available: {list(TOOL_DESCRIPTIONS)}",
                1,
            )
