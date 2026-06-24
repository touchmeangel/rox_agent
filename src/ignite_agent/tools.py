from __future__ import annotations

import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from html import unescape
from pathlib import Path

from ddgs import DDGS

from ignite_agent.debug_logger import DebugLogger, default_debug_logger
from ignite_agent.rolling_window import RollingWindow
from ignite_agent.search_state import SearchState
from ignite_agent.tool_result import ToolResult
import json
import hashlib

def _smart_process_output(stdout: str, tool_name: str, project_path: str, limit: int) -> str:
    if len(stdout) <= limit:
        return stdout

    run_hash = hashlib.md5(stdout.encode()).hexdigest()[:6]
    
    try:
        data = json.loads(stdout)
        log_file = f".ignite_{tool_name}_{run_hash}.json"
        log_path = Path(project_path) / log_file
        
        log_path.write_text(stdout, encoding="utf-8")

        summary_data = {}
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, list) and len(v) > 5:
                    summary_data[k] = v[:5] + [f"... <{len(v) - 5} more items omitted in {log_file}>"]
                else:
                    summary_data[k] = v
        elif isinstance(data, list) and len(data) > 5:
            summary_data = data[:5] + [f"... <{len(data) - 5} more items omitted in {log_file}>"]
        else:
            summary_data = data
            
        summary_str = json.dumps(summary_data, indent=2)
        
        prompt_msg = (
            f"[SYSTEM: Massive JSON output intercepted ({len(stdout)} chars).]\n"
            f"[Full raw JSON saved to '{log_file}'. Use `shell` with `jq` to query the full file if needed.]\n"
            f"[Preview below:]\n\n"
            f"{summary_str}"
        )
        
        return _truncate(prompt_msg, limit, "json_summary")
    except json.JSONDecodeError:
        pass

    log_file = f".ignite_{tool_name}_{run_hash}.log"
    log_path = Path(project_path) / log_file
    log_path.write_text(stdout, encoding="utf-8")

    preview_size = 3000
    head = stdout[:preview_size]
    tail = stdout[-preview_size:] if len(stdout) > (preview_size * 2) else ""

    prompt_msg = (
        f"[SYSTEM: Output exceeded context limits ({len(stdout)} chars). Full output saved to '{log_file}'.]\n"
        f"[Do NOT repeat this exact command. Use the `shell` tool with `rg` (ripgrep) to search '{log_file}', or `tail` to view specific errors.]\n\n"
        f"--- PREVIEW (First {preview_size} chars) ---\n{head}\n"
    )
    if tail:
        prompt_msg += f"\n--- TAIL (Last {preview_size} chars) ---\n{tail}\n"

    return prompt_msg


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

        result = subprocess.run(  # noqa: S603 isolated docker container
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
    result = run_command(
        f"slither {args}", cwd=project_path, timeout=240, window=window
    )

    if not result.success:
        data = result.json_data()
        if isinstance(data, dict) and data.get("success") is True:
            return ToolResult(result.command, result.stdout, result.stderr, 0)

    return result


def aderyn(
    project_path: str, flags: str = "", window: RollingWindow | None = None
) -> ToolResult:
    cmd_str = f"aderyn {flags}".strip() if flags else "aderyn"
    return run_command(cmd_str, cwd=project_path, timeout=180, window=window)


def myth(
    project_path: str, flags: str = "", window: RollingWindow | None = None
) -> ToolResult:
    cmd = f"myth {flags}".strip() if flags else "myth analyze ./"
    return run_command(cmd, cwd=project_path, timeout=300, window=window)


def cast(
    project_path: str, subcommand: str, window: RollingWindow | None = None
) -> ToolResult:
    return run_command(
        f"cast {subcommand}", cwd=project_path, timeout=30, window=window
    )


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
            f"cat {relative_path}",
            "",
            f"File not found: {relative_path}. Use list_sol_files or shell('find . -iname ...') "
            f"to locate the right path — relative paths are resolved against the project root.",
            1,
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
        return ToolResult(
            label,
            "",
            f"Search failed after retry: {last_exc}. Try a shorter or differently-worded "
            f"query rather than retrying this exact one.",
            1,
        )

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    if not results:
        return ToolResult(label, f"(no results — today is {today})", "", 0)

    lines = [f"(search run {today})\n"]
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
            f"Refused: scheme '{scheme or '(none)'}' is not allowed — only http/https URLs can be fetched. "
            f"Pass the exact URL string from a web_search result.",
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
        return ToolResult(
            label,
            "",
            f"HTTP {exc.code} fetching {url} — try a web_search instead of guessing a different URL.",
            1,
        )
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


TOOL_SPECS: list[dict] = [
    {
        "name": "slither",
        "description": (
            "Run Slither static analysis against the project.\n\n"
            "NOTE: Slither exits non-zero (usually 255) when it finds issues — that is "
            "NOT a failure. The harness normalises this: if the JSON output shows "
            "'success: true', the call is treated as successful regardless of exit code. "
            "Always check stdout for JSON even if the tool is reported as failed.\n\n"
            "Pass '--json -' for machine-readable output you can parse; the default "
            "text report is easier to scan visually. Use '--exclude-dependencies' to "
            "skip library code."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "flags": {
                    "type": "string",
                    "description": "CLI flags + target, e.g. './ --json -' or './ --exclude-dependencies --detect reentrancy-eth'.",
                    "default": "./",
                }
            },
        },
    },
    {
        "name": "aderyn",
        "description": (
            "Run Aderyn Rust-based static analysis against the project root.\n\n"
            "NOTE: Aderyn outputs its analysis report into locally saved files in your "
            "current working workspace directory rather than directly returning them via stdout streams.\n"
            "By default, running it directly produces a 'report.md' file.\n\n"
            "To generate structural JSON outputs for backend parsing, pass the layout output path flag "
            "explictly: '-o aderyn_report.json'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "flags": {
                    "type": "string",
                    "description": "CLI argument configurations, e.g., '-o aderyn_report.json'.",
                    "default": "",
                }
            },
        },
    },
    {
        "name": "myth",
        "description": (
            "Run Mythril symbolic execution against specific Solidity files.\n\n"
            "Unlike Slither/Aderyn, Mythril uses SMT-based symbolic execution so it "
            "catches vulnerabilities that static pattern matching misses: integer "
            "overflows/underflows, assertion violations, reachable selfdestruct or "
            "delegatecall, and some reentrancy classes.\n\n"
            "Always pass '-o json' for machine-readable results and "
            "'--execution-timeout <n>' to avoid hanging on complex contracts.\n\n"
            "Example flags:\n"
            "  'analyze src/Vault.sol -o json --execution-timeout 60'\n"
            "  'analyze src/A.sol src/B.sol -o json --execution-timeout 60 --max-depth 12'\n\n"
            "NOTE: Mythril is slow. Keep the file list short and always set a timeout. "
            "SWC-107 = reentrancy, SWC-101 = integer overflow, SWC-106 = selfdestruct, "
            "SWC-112 = delegatecall — see swcregistry.io for the full list."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "flags": {
                    "type": "string",
                    "description": (
                        "Full myth invocation after 'myth', e.g. "
                        "'analyze src/Vault.sol -o json --execution-timeout 60'."
                    ),
                    "default": "",
                }
            },
        },
    },
    {
        "name": "forge",
        "description": (
            "Run Foundry's forge. Use 'build' to confirm the project compiles before "
            "trusting any analysis of it, 'test -vvv' to run the test suite with verbose "
            "traces, or 'coverage' to see which branches tests actually exercise. A clean "
            "build is a prerequisite for trusting slither output — if build fails, fix or "
            "note that before treating static-analysis results as reliable."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subcommand": {
                    "type": "string",
                    "description": "forge subcommand + flags, e.g. 'build', 'test -vvv', 'coverage'.",
                    "default": "build",
                }
            },
        },
    },
    {
        "name": "cast",
        "description": (
            "Foundry's Swiss-army knife for EVM data operations. Use this instead of shell "
            "for anything involving ABIs, selectors, calldata, or on-chain reads.\n\n"
            "Most useful operations:\n"
            "  cast 4byte <selector>                  — reverse-lookup a function selector\n"
            "  cast sig 'transfer(address,uint256)'   — compute a function selector\n"
            "  cast abi-decode 'func(uint,addr)' <hex>  — decode calldata or return data\n"
            "  cast abi-encode 'func(uint,addr)' 1 0x.. — encode a call\n"
            "  cast disassemble <bytecode-hex>        — disassemble EVM bytecode\n"
            "  cast storage <addr> <slot> --rpc-url <url>  — read a storage slot on-chain\n"
            "  cast call <addr> 'balanceOf(address)' <arg> --rpc-url <url>  — call a view fn\n"
            "  cast keccak 'Transfer(address,address,uint256)'  — compute a topic hash\n\n"
            "For on-chain reads, you need --rpc-url. Use --rpc-url $RPC_URL if the env var is set."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subcommand": {
                    "type": "string",
                    "description": "Full cast invocation after 'cast', e.g. '4byte 0xa9059cbb' or 'sig \"transfer(address,uint256)\"'.",
                }
            },
            "required": ["subcommand"],
        },
    },
    {
        "name": "git",
        "description": (
            "Run read-only git commands to understand project history — when a suspicious "
            "pattern was introduced, who touched a file, what changed in the last commit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "args": {
                    "type": "string",
                    "description": "git subcommand + flags, e.g. 'log --oneline -20' or 'log -p -- src/Vault.sol'.",
                    "default": "log --oneline -10",
                }
            },
        },
    },
    {
        "name": "read_file",
        "description": "Read one source file in full. path is relative to the project root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the project root, e.g. 'src/Vault.sol'.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_sol_files",
        "description": "List Solidity source files matching a glob, to orient yourself before reading specific files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern relative to the project root.",
                    "default": "**/*.sol",
                }
            },
        },
    },
    {
        "name": "shell",
        "description": (
            "Run an arbitrary shell command for anything the other tools don't cover. "
            "Prefer the dedicated tools (slither/aderyn/myth/forge/cast/git/read_file) "
            "when they fit — reach for shell when you genuinely need composition.\n\n"
            "Available utilities beyond the standard toolbox:\n"
            "  rg <pattern> [path]     — fast ripgrep; use instead of grep for multi-file search\n"
            "  fd <pattern> [path]     — fast find with cleaner syntax\n"
            "  jq <filter>             — JSON processing for tool output\n"
            "  cloc [path]             — count lines of code to scope a codebase\n"
            "  tree [path]             — directory structure at a glance\n"
            "  xxd <file>              — hex dump for bytecode inspection\n"
            "  strings <file>          — extract printable strings from binaries\n"
            "  solhint [path]          — Solidity linter for style/best-practice issues\n"
            "  node -e '<js>'          — quick JS evaluation (keccak, ABI, hex conversions)\n"
            "  solc-select use <ver>   — switch compiler version if the project needs it"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Full shell command string.",
                }
            },
            "required": ["command"],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Search the web for security-relevant information you don't have locally."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "3-6 word query.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of results to return.",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_fetch",
        "description": (
            "Fetch the full text of one specific URL — use after web_search when a result's "
            "snippet isn't enough."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Exact URL to fetch, taken from a prior web_search result.",
                }
            },
            "required": ["url"],
        },
    },
]

DISPATCHABLE_TOOL_NAMES: frozenset[str] = frozenset(spec["name"] for spec in TOOL_SPECS)


def _truncate(text: str, limit: int, what: str) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return (
        text[:limit] + f"\n\n... [{what} truncated — {omitted} more chars omitted. "
        f"Use a more targeted command (narrower flags, grep, head/tail) to see more.]"
    )


_STDOUT_LIMITS: dict[str, int] = {
    "myth": 200_000,
    "slither": 200_000,
    "aderyn": 200_000,
}
_DEFAULT_STDOUT_LIMIT = 10_000


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
    tool_input: dict,
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

    args_repr = ", ".join(f"{k}={v!r}" for k, v in tool_input.items())

    try:
        window.feed(f"⎿  {tool_name}  {args_repr}\n")

        match tool_name:
            case "slither":
                result = slither(
                    project_path, tool_input.get("flags") or "./", window=window
                )
            case "aderyn":
                result = aderyn(
                    project_path, tool_input.get("flags") or "", window=window
                )
            case "myth":
                result = myth(
                    project_path, tool_input.get("flags") or "", window=window
                )
            case "forge":
                result = forge(
                    project_path, tool_input.get("subcommand") or "build", window=window
                )
            case "cast":
                subcommand = tool_input.get("subcommand", "")
                result = (
                    cast(project_path, subcommand, window=window)
                    if subcommand
                    else ToolResult("cast", "", "Missing required 'subcommand'.", 1)
                )
            case "git":
                result = git(
                    project_path,
                    tool_input.get("args") or "log --oneline -10",
                    window=window,
                )
            case "read_file":
                path = tool_input.get("path", "")
                result = (
                    read_file(project_path, path)
                    if path
                    else ToolResult("read_file", "", "Missing required 'path'.", 1)
                )
            case "list_sol_files":
                result = list_sol_files(
                    project_path, tool_input.get("pattern") or "**/*.sol"
                )
            case "shell":
                command = tool_input.get("command", "")
                result = (
                    shell(project_path, command, window=window)
                    if command
                    else ToolResult("shell", "", "Missing required 'command'.", 1)
                )
            case "web_search":
                query = tool_input.get("query", "")
                max_results = tool_input.get("max_results", 5)
                result = (
                    search_state.consume(
                        "web_search", query, lambda: web_search(query, max_results)
                    )
                    if search_state is not None
                    else web_search(query, max_results)
                )
            case "web_fetch":
                url = tool_input.get("url", "")
                result = (
                    search_state.consume("web_fetch", url, lambda: web_fetch(url))
                    if search_state is not None
                    else web_fetch(url)
                )
            case _:
                result = ToolResult(
                    tool_name,
                    "",
                    f"Unknown tool '{tool_name}'. Available: {sorted(DISPATCHABLE_TOOL_NAMES)}",
                    1,
                )

        stdout_limit = _STDOUT_LIMITS.get(tool_name, _DEFAULT_STDOUT_LIMIT)        
        smart_stdout = _smart_process_output(result.stdout, tool_name, project_path, stdout_limit)
        smart_stderr = _truncate(result.stderr, 4_000, "stderr")

        result = ToolResult(
            result.command,
            smart_stdout,
            smart_stderr,
            result.returncode,
            is_truncation_system_prompt=True
        )

        debug_logger.log(tool_name, args_repr, result)
        window.feed(f"\n   {_summary(result)}\n")
    finally:
        if owns_window:
            window.__exit__(None, None, None)

    return result
