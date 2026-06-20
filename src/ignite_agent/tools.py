from __future__ import annotations

import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path

from ddgs import DDGS

from ignite_agent.debug_logger import DebugLogger
from ignite_agent.rolling_window import RollingWindow
from ignite_agent.search_state import SearchState
from ignite_agent.tool_result import ToolResult

default_debug_logger = DebugLogger()


def _stream_subprocess(
    args: list[str],
    cwd: str,
    timeout: int,
    window: RollingWindow,
) -> tuple[str, str, int]:
    proc = subprocess.Popen(  # noqa: S603, isolated docker environment
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
        args = ["bash", "-c", command]
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


def _web_search_ddgs(query: str, max_results: int = 5) -> ToolResult:
    query = query.strip()
    label = f"web_search {query!r}"
    if not query:
        return ToolResult(label, "", "Empty query.", 1)

    results = None
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            results = DDGS().text(query, max_results=max_results, backend="auto")
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(1.5)

    if last_exc is not None:
        return ToolResult(label, "", f"Search failed after retry: {last_exc}", 1)
    if not results:
        return ToolResult(label, "(no results)", "", 0)

    lines = []
    for i, r in enumerate(results, 1):
        lines.append(
            f"[{i}] {r.get('title', '')}\n"
            f"    {r.get('href', '')}\n"
            f"    {r.get('body', '')[:400]}\n"
        )
    return ToolResult(label, "\n".join(lines), "", 0)


def web_search(query: str, max_results: int = 5) -> ToolResult:
    return _web_search_ddgs(query, max_results)


def web_fetch(url: str, max_chars: int = 6000) -> ToolResult:
    """Fetch a URL and return its readable text content (tags stripped)."""
    url = url.strip()
    label = f"web_fetch {url}"

    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        return ToolResult(
            label,
            "",
            f"Refused: scheme '{scheme or '(none)'}' is not allowed — only http/https URLs can be fetched.",
            1,
        )

    req = urllib.request.Request(  # noqa: S310 - allowlist above
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; ignite-agent/1.0)"}
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - allowlist above
            raw = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            html_text = raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        return ToolResult(label, "", f"HTTP {exc.code}", 1)
    except urllib.error.URLError as exc:
        return ToolResult(label, "", f"Network error: {exc.reason}", 1)
    except Exception as exc:
        return ToolResult(label, "", str(exc), 1)

    html_text = re.sub(
        r"(?is)<(script|style|nav|footer|header)\b.*?</\1>", " ", html_text
    )
    text = re.sub(r"(?s)<[^>]+>", " ", html_text)
    text = unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    text = text.strip()

    note = ""
    if len(text) > max_chars:
        text = text[:max_chars]
        note = f"\n\n... [truncated to {max_chars} chars — fetch a narrower URL/section if you need more]"

    return ToolResult(label, text + note, "", 0)


TOOL_DESCRIPTIONS: dict[str, str] = {
    "slither": "Run slither static analysis. args: extra flags e.g. './ --json -'",
    "forge": "Run forge. args: subcommand + flags e.g. 'build', 'coverage', 'test -vvv'",
    "git": "Run git. args: subcommand e.g. 'log --oneline -20'",
    "read_file": "Read a source file. args: path relative to project root e.g. 'src/Vault.sol'",
    "list_sol_files": "List all .sol files. args: optional glob default '**/*.sol'",
    "shell": "Run any shell command. args: full command string",
    "web_search": (
        "Search the web — error messages, library/tool docs, CVEs, version-specific "
        "behavior. args: a short specific query (3-8 words). Put the exact error "
        "string or exception name in the query, not a vague description — e.g. "
        "'PermissionError .solc-select/artifacts docker' not 'why does slither "
        "not work'. Narrow with site: when you know the best source, e.g. "
        "'site:github.com/crytic/slither <error>'. If results aren't relevant, "
        "narrow the query and search again rather than giving up. Budget is "
        "limited per step — make each query count."
    ),
    "web_fetch": (
        "Fetch the full text of one specific URL — use after web_search when a "
        "result's snippet isn't enough (e.g. a GitHub issue thread, a full docs "
        "page, a changelog). args: the exact URL from a web_search result. "
        "Counts against the same per-step search budget as web_search."
    ),
}


def _summary(result: ToolResult) -> str:
    total = len(result.stdout) + len(result.stderr)
    size = f"{total / 1024:.1f} KB" if total >= 1024 else f"{total} B"

    if result.success:
        if total == 0:
            return "[green]✓[/green]  (no output)"
        if total < 400:
            raw = result.stdout if result.stdout else result.stderr
            snippet = next(
                (line.strip() for line in raw.splitlines() if line.strip()), ""
            )
            if snippet:
                return f"[green]✓[/green]  {size}  [dim]·  {snippet[:72]}[/dim]"
        return f"[green]✓[/green]  {size}"

    return f"[red]✗[/red]  exit {result.returncode}  {size}"


def dispatch(
    tool_name: str,
    args: str,
    project_path: str,
    window: RollingWindow | None = None,
    search_state: SearchState | None = None,
    debug_logger: DebugLogger | None = None,
) -> ToolResult:
    debug_logger = debug_logger or default_debug_logger

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
            case "web_search":
                result = (
                    search_state.consume("web_search", args, lambda: web_search(args))
                    if search_state is not None
                    else web_search(args)
                )
            case "web_fetch":
                result = (
                    search_state.consume("web_fetch", args, lambda: web_fetch(args))
                    if search_state is not None
                    else web_fetch(args)
                )
            case _:
                result = ToolResult(
                    tool_name,
                    "",
                    f"Unknown tool '{tool_name}'. Available: {list(TOOL_DESCRIPTIONS)}",
                    1,
                )

        debug_logger.log(tool_name, args, result)
        window.feed(f"\n   {_summary(result)}\n")
    finally:
        if owns_window:
            window.__exit__(None, None, None)

    return result
