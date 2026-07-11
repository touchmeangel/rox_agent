from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from html import unescape
from pathlib import Path

import docx
import openpyxl

from ddgs import DDGS
from playwright.sync_api import sync_playwright

from ignite_core.debug_logger import DebugLogger, default_debug_logger
from ignite_core.reporting import Reporter, RichReporter
from ignite_core.rolling_window import RollingWindow
from ignite_core.search_state import SearchState
from ignite_core.storages.download_store import DownloadStore
from ignite_core.storages.overflow_store import OverflowStore
from ignite_core.tool_result import ToolResult
from ignite_core.graph import Graph

_overflow_store = OverflowStore()

_download_store = DownloadStore()
_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024  # 100 MB safety cap


def _tool_display(tool_name: str, tool_input: dict) -> str:
    match tool_name:
        case "slither":
            display = f"slither {tool_input.get('flags', './')}"
        case "aderyn":
            display = f"aderyn {tool_input.get('flags', '') or '.'}"
        case "forge":
            display = f"forge {tool_input.get('subcommand', 'build')}"
        case "cast":
            display = f"cast {tool_input.get('subcommand', '')}"
        case "git":
            display = f"git {tool_input.get('args', 'log --oneline -10')}"
        case "read_file":
            display = f"cat {tool_input.get('path', '')}"
        case "list_sol_files":
            display = f"find . -name '{tool_input.get('pattern', '**/*.sol')}'"
        case "shell":
            cmd = tool_input.get("command", "")
            display = cmd[:120] + ("…" if len(cmd) > 120 else "")
        case "web_search":
            display = f'web_search  "{tool_input.get("query", "")}"'
        case "web_fetch":
            display = f"web_fetch  {tool_input.get('url', '')}"
        case "browser_fetch":
            display = f"browser_fetch  {tool_input.get('url', '')}"
        case "download_file":
            display = f"download_file  {tool_input.get('url', '')}"
        case "read_document":
            display = f"read_document  {tool_input.get('source', '')}"
        case "github_search":
            display = f'github_search [{tool_input.get("search_type", "repositories")}]  "{tool_input.get("query", "")}"'
        case _:
            display = f"{tool_name}  {tool_input}"
    return display.replace("\r", "").replace("\n", " ")


def _smart_process_output(
    stdout: str, tool_name: str, project_path: str, limit: int
) -> str:
    if len(stdout) <= limit:
        return stdout

    run_hash = hashlib.sha256(stdout.encode()).hexdigest()[:6]

    try:
        data = json.loads(stdout)
        log_path = _overflow_store.write(
            stdout, f"ignite_{tool_name}_{run_hash}", ".json"
        )
        log_file = log_path.name

        summary_data = {}
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, list) and len(v) > 5:
                    summary_data[k] = v[:5] + [
                        f"... <{len(v) - 5} more items omitted in {log_file}>"
                    ]
                else:
                    summary_data[k] = v
        elif isinstance(data, list) and len(data) > 5:
            summary_data = data[:5] + [
                f"... <{len(data) - 5} more items omitted in {log_file}>"
            ]
        else:
            summary_data = data

        summary_str = json.dumps(summary_data, indent=2)
        prompt_msg = (
            f"[SYSTEM: Massive JSON output intercepted ({len(stdout)} chars).]\n"
            f"[Full raw JSON saved to '{log_path}'. Use `shell` with `jq` to query the full file if needed.]\n"
            f"[Preview below:]\n\n{summary_str}"
        )
        return _truncate(
            prompt_msg, limit, "The full file path is already noted above."
        )
    except json.JSONDecodeError:
        pass

    log_path = _overflow_store.write(stdout, f"ignite_{tool_name}_{run_hash}", ".json")
    log_file = log_path.name

    preview_size = 3000
    head = stdout[:preview_size]
    tail = stdout[-preview_size:] if len(stdout) > (preview_size * 2) else ""

    prompt_msg = (
        f"[SYSTEM: Output exceeded context limits ({len(stdout)} chars). Full output saved to '{log_file}'.]\n"
        f"[Do NOT repeat this exact command. Use `shell` with `rg` to search '{log_path}', or `tail` to view specific errors.]\n\n"
        f"--- PREVIEW (first {preview_size} chars) ---\n{head}\n"
    )
    if tail:
        prompt_msg += f"\n--- TAIL (last {preview_size} chars) ---\n{tail}\n"

    return prompt_msg


def _stream_subprocess(
    args: list[str],
    cwd: str,
    timeout: int,
    reporter: Reporter,
) -> tuple[str, str, int]:
    proc = subprocess.Popen(  # noqa: S603 — isolated docker environment
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
                    display = line.rstrip("\r\n")
                    if display:
                        reporter.stream_line(display)
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
    reporter: Reporter | None = None,
) -> ToolResult:
    try:
        args = ["bash", "-c", command]
        if reporter is not None:
            stdout, stderr, returncode = _stream_subprocess(
                args, cwd, timeout, reporter
            )
            return ToolResult(command, stdout, stderr, returncode)

        result = subprocess.run(  # noqa: S603 — isolated docker container
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
    project_path: str, args: str = "./", reporter: Reporter | None = None
) -> ToolResult:
    result = run_command(
        f"slither {args}", cwd=project_path, timeout=240, reporter=reporter
    )
    if not result.success:
        data = result.json_data()
        if isinstance(data, dict) and data.get("success") is True:
            return ToolResult(result.command, result.stdout, result.stderr, 0)
    return result


def aderyn(
    project_path: str, flags: str = "", reporter: Reporter | None = None
) -> ToolResult:
    cmd_str = f"aderyn {flags}".strip() if flags else "aderyn"
    return run_command(cmd_str, cwd=project_path, timeout=180, reporter=reporter)


def cast(
    project_path: str, subcommand: str, reporter: Reporter | None = None
) -> ToolResult:
    return run_command(
        f"cast {subcommand}", cwd=project_path, timeout=30, reporter=reporter
    )


def forge(
    project_path: str, args: str = "build", reporter: Reporter | None = None
) -> ToolResult:
    return run_command(
        f"forge {args}", cwd=project_path, timeout=180, reporter=reporter
    )


def evm_decode_constructor(
    project_path: str,
    creation_input: str,
    constructor_types: str,
    reporter: Reporter | None = None,
) -> ToolResult:
    return run_command(
        f"evm_decode_constructor {creation_input} {constructor_types}",
        cwd=project_path,
        timeout=180,
        reporter=reporter,
    )


def git(
    project_path: str,
    args: str = "log --oneline -10",
    reporter: Reporter | None = None,
) -> ToolResult:
    return run_command(f"git {args}", cwd=project_path, timeout=30, reporter=reporter)


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
    project_path: str, command: str, reporter: Reporter | None = None
) -> ToolResult:
    return run_command(command, cwd=project_path, timeout=120, reporter=reporter)


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
            f"Search failed after retry: {last_exc}. Try a shorter/differently-worded query, "
            f"or use browser_fetch on 'https://search.brave.com/search?q={urllib.parse.quote(query)}"
            f"&source=web' as a fallback.",
            1,
        )

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    if not results:
        return ToolResult(
            label,
            f"(no results — today is {today}). If that seems wrong, try browser_fetch on "
            f"'https://search.brave.com/search?q={urllib.parse.quote(query)}&source=web' instead.",
            "",
            0,
        )

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


def github_search(
    query: str, search_type: str = "repositories", max_results: int = 5
) -> ToolResult:
    query = query.strip()
    label = f"github_search [{search_type}] {query!r}"
    if not query:
        return ToolResult(label, "", "Empty query.", 1)
    if search_type not in ("repositories", "code"):
        return ToolResult(label, "", "search_type must be 'repositories' or 'code'.", 1)

    endpoint = f"https://api.github.com/search/{search_type}"
    params = urllib.parse.urlencode(
        {"q": query, "per_page": max(1, min(max_results, 10))}
    )
    url = f"{endpoint}?{params}"

    req = urllib.request.Request(  # noqa: S310
        url,
        headers={
            "User-Agent": "ignite-agent/1.0",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            return ToolResult(
                label,
                "",
                "GitHub API rate limit hit (unauthenticated: ~10 search req/min, ~60 req/hr). "
                "Wait before retrying, or fall back to web_search.",
                1,
            )
        return ToolResult(label, "", f"HTTP {exc.code} from GitHub API.", 1)
    except Exception as exc:
        return ToolResult(label, "", str(exc), 1)

    items = data.get("items", [])
    if not items:
        return ToolResult(label, "(no results)", "", 0)

    lines: list[str] = []
    if search_type == "repositories":
        for i, r in enumerate(items, 1):
            lines.append(
                f"[{i}] {r.get('full_name')}  ★{r.get('stargazers_count', 0)}\n"
                f"    {r.get('html_url')}\n"
                f"    {(r.get('description') or '')[:200]}\n"
            )
    else:
        for i, r in enumerate(items, 1):
            repo_name = (r.get("repository") or {}).get("full_name", "?")
            lines.append(
                f"[{i}] {repo_name} :: {r.get('path')}\n    {r.get('html_url')}\n"
            )

    return ToolResult(label, "\n".join(lines), "", 0)


def _truncate(text: str, limit: int, note: str) -> str:
    """Hard-cut `text` at `limit` chars and append `note` explaining what got
    cut and how to see the rest. Every truncation in this module — tool
    stdout/stderr, extracted document text — funnels through here so there's
    one place that decides what a truncation notice looks like."""
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit] + f"\n\n...[truncated — {omitted} more chars omitted. {note}]"


def _pdftotext(path: Path, max_chars: int) -> tuple[str, str]:
    if shutil.which("pdftotext") is None:
        return "", (
            "pdftotext not found in PATH. The Docker image needs poppler-utils: "
            "add `apt-get install -y poppler-utils` to the Dockerfile."
        )
    try:
        result = subprocess.run(  # noqa: S603
            ["pdftotext", "-layout", str(path), "-"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return "", "pdftotext timed out after 60s — the PDF may be unusually large."
    except Exception as exc:
        return "", str(exc)

    if result.returncode != 0:
        return (
            "",
            f"pdftotext failed (exit {result.returncode}): {result.stderr.strip()[:500]}",
        )

    text = result.stdout.strip()
    if not text:
        return "", (
            "pdftotext produced no text — this is likely a scanned/image-only PDF. "
            "OCR is not supported; try finding an HTML version of the same report instead."
        )

    text = _truncate(text, max_chars, f"See the full file at {path}.")

    return text, ""


def download_file(url: str, filename: str | None = None) -> ToolResult:
    url = url.strip()
    label = f"download_file {url}"

    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        return ToolResult(
            label,
            "",
            f"Refused: scheme '{scheme or '(none)'}' is not allowed — only http/https URLs.",
            1,
        )

    req = urllib.request.Request(  # noqa: S310
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; ignite-agent/1.0)"}
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            content_type = resp.headers.get_content_type() or "application/octet-stream"
            raw = resp.read(_MAX_DOWNLOAD_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return ToolResult(label, "", f"HTTP {exc.code} downloading {url}", 1)
    except urllib.error.URLError as exc:
        return ToolResult(label, "", f"Network error: {exc.reason}", 1)
    except Exception as exc:
        return ToolResult(label, "", str(exc), 1)

    if len(raw) > _MAX_DOWNLOAD_BYTES:
        return ToolResult(
            label,
            "",
            f"File exceeds {_MAX_DOWNLOAD_BYTES // (1024 * 1024)}MB safety limit — refused.",
            1,
        )

    if not filename:
        url_path = urllib.parse.urlsplit(url).path
        filename = Path(url_path).name or "download"
    if "." not in filename:
        filename += mimetypes.guess_extension(content_type) or ""

    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)[:120]
    dest = _download_store.write_bytes(
        raw, f"{hashlib.sha256(url.encode()).hexdigest()[:8]}_{safe_name}"
    )
    dest.write_bytes(raw)

    size_kb = len(raw) / 1024
    return ToolResult(
        label,
        f"Downloaded {size_kb:.1f} KB to: {dest}\n"
        f"Content-Type: {content_type}\n"
        f"Use read_document on this path if it's a PDF or any other document file, or shell/read_file for other formats.",
        "",
        0,
    )


def _resolve_source_path(
    source: str, label: str
) -> tuple[Path | None, ToolResult | None]:
    if source.startswith(("http://", "https://")):
        dl = download_file(source)
        if not dl.success:
            return None, ToolResult(
                label, "", f"Could not download file: {dl.stderr}", 1
            )
        match = re.search(r"Downloaded .* to: (\S+)", dl.stdout)
        path = Path(match.group(1)) if match else None
    else:
        candidate = Path(source)
        path = candidate if candidate.exists() else (_download_store.path() / source)

    if not path or not path.exists():
        return None, ToolResult(
            label,
            "",
            f"File not found: {source}. Pass a URL, or a local path returned by download_file.",
            1,
        )
    return path, None


def _extract_docx(path: Path, max_chars: int) -> tuple[str, str]:
    try:
        document = docx.Document(str(path))
    except Exception as exc:
        return "", f"Failed to open .docx: {exc}"

    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))

    text = "\n".join(parts).strip()
    if not text:
        return "", "No extractable text found in .docx (it may be empty or image-only)."
    return _truncate(text, max_chars, f"See the full file at {path}."), ""


def _extract_xlsx(path: Path, max_chars: int) -> tuple[str, str]:
    try:
        wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
    except Exception as exc:
        return "", f"Failed to open spreadsheet: {exc}"

    parts = []
    for sheet in wb.worksheets:
        parts.append(f"--- Sheet: {sheet.title} ---")
        for row in sheet.iter_rows(values_only=True):
            if row is None or all(c is None for c in row):
                continue
            parts.append(" | ".join("" if c is None else str(c) for c in row))

    text = "\n".join(parts).strip()
    if not text:
        return "", "No data found in spreadsheet."
    return _truncate(text, max_chars, f"See the full file at {path}."), ""


def _extract_csv(path: Path, max_chars: int) -> tuple[str, str]:
    try:
        text = path.read_text(errors="replace")
    except Exception as exc:
        return "", str(exc)
    if not text.strip():
        return "", "CSV/TSV file is empty."
    return _truncate(text, max_chars, f"See the full file at {path}."), ""


_DOC_DISPATCH = {
    ".pdf": _pdftotext,
    ".docx": _extract_docx,
    ".xlsx": _extract_xlsx,
    ".xlsm": _extract_xlsx,
    ".csv": _extract_csv,
    ".tsv": _extract_csv,
}

_CONTENT_TYPE_EXT: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "text/csv": ".csv",
    "text/tab-separated-values": ".tsv",
}


def _guess_doc_ext(url: str, content_type: str) -> str | None:
    ext = _CONTENT_TYPE_EXT.get(content_type)
    if ext:
        return ext
    url_ext = Path(urllib.parse.urlsplit(url).path).suffix.lower()
    return url_ext if url_ext in _DOC_DISPATCH else None


def read_document(source: str, max_chars: int = 15_000) -> ToolResult:
    source = source.strip()
    label = f"read_document {source}"
    path, err = _resolve_source_path(source, label)
    if err:
        return err

    ext = path.suffix.lower()
    extractor = _DOC_DISPATCH.get(ext)
    if extractor is None:
        return ToolResult(
            label,
            "",
            f"Unsupported extension '{ext}'. Supported: {sorted(_DOC_DISPATCH)}. "
            f"For .sol/.json/.md/.txt/.log, use read_file or shell('cat ...') instead.",
            1,
        )

    text, error = extractor(path, max_chars)
    if error:
        return ToolResult(label, "", error, 1)
    return ToolResult(label, text, "", 0)


def browser_fetch(url: str, max_chars: int = 8_000, wait_ms: int = 0) -> ToolResult:
    url = url.strip()
    label = f"browser_fetch {url}"

    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        return ToolResult(
            label, "", f"Refused: scheme '{scheme or '(none)'}' is not allowed.", 1
        )

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            try:
                page = browser.new_page(
                    user_agent="Mozilla/5.0 (compatible; ignite-agent/1.0)"
                )
                page.goto(url, timeout=25_000, wait_until="networkidle")
                if wait_ms:
                    page.wait_for_timeout(wait_ms)
                text = page.inner_text("body")
            finally:
                browser.close()
    except Exception as exc:
        return ToolResult(label, "", f"Browser fetch failed: {exc}", 1)

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    if not text:
        return ToolResult(label, "", "Page rendered but produced no visible text.", 1)

    note = ""
    if len(text) > max_chars:
        text = text[:max_chars]
        note = f"\n\n... [truncated to {max_chars} chars]"
    return ToolResult(label, text + note, "", 0)


def web_fetch(url: str, max_chars: int = 6000) -> ToolResult:
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

    req = urllib.request.Request(  # noqa: S310
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; ignite-agent/1.0)"}
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            content_type = resp.headers.get_content_type() or ""
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        return ToolResult(
            label, "", f"HTTP {exc.code} fetching {url} — try a web_search instead.", 1
        )
    except urllib.error.URLError as exc:
        return ToolResult(label, "", f"Network error: {exc.reason}", 1)
    except Exception as exc:
        return ToolResult(label, "", str(exc), 1)

    doc_ext = _guess_doc_ext(url, content_type)
    if doc_ext:
        tmp_path = _download_store.write_bytes(
            raw, f"{hashlib.sha256(url.encode()).hexdigest()[:8]}{doc_ext}"
        )
        text, error = _DOC_DISPATCH[doc_ext](tmp_path, max_chars)
        if error:
            return ToolResult(
                label, "", f"Detected {doc_ext} but extraction failed: {error}", 1
            )
        return ToolResult(
            label, f"[{doc_ext} detected — extracted automatically]\n\n{text}", "", 0
        )

    html_text = raw.decode(charset, errors="replace")
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
        note = f"\n\n... [truncated to {max_chars} chars]"

    return ToolResult(label, text + note, "", 0)


TOOL_SPECS: list[dict] = [
    {
        "name": "graph_query",
        "description": (
            "Query the pre-built structural graph of this repo (contracts, inheritance, "
            "function call graph, storage reads/writes) instead of grepping or reading "
            "files to answer structural questions. Cheaper and more reliable than "
            "shell/read_file for these. Not available every run — check for an error."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "e.g. 'Vault' or 'Vault.withdraw'. Ignored for dead_code."},
                "relation": {
                    "type": "string",
                    "enum": ["callers", "callees", "inherits", "inherited_by", "dead_code", "search"],
                },
            },
            "required": ["relation"],
        },
    },
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
            "Foundry's Swiss-army knife for EVM data operations.\n\n"
            "Most useful operations:\n"
            "  cast 4byte <selector>                  — reverse-lookup a function selector\n"
            "  cast sig 'transfer(address,uint256)'   — compute a function selector\n"
            "  cast abi-decode 'func(uint,addr)' <hex>  — decode calldata\n"
            "  cast abi-encode 'func(uint,addr)' 1 0x.. — encode a call\n"
            "  cast disassemble <bytecode-hex>        — disassemble EVM bytecode\n"
            "  cast keccak 'Transfer(address,address,uint256)'  — compute a topic hash"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subcommand": {
                    "type": "string",
                    "description": "Full cast invocation after 'cast', e.g. '4byte 0xa9059cbb'.",
                }
            },
            "required": ["subcommand"],
        },
    },
    {
        "name": "evm_decode_constructor",
        "description": (
            "Decode Solidity constructor arguments from EVM contract creation bytecode. "
            "Useful when auditing deployed contracts whose constructor parameters are not "
            "available in the verified source or when recovering secrets passed to constructors.\n\n"
            "Usage:\n"
            "  evm_decode_constructor <creation_input> <constructor_types>\n\n"
            "Example:\n"
            '  evm_decode_constructor "0x6080604..." "address,uint256"\n\n'
            "Supported ABI types include address, bool, string, bytes, bytes32, "
            "uint/int variants, fixed-size arrays, dynamic arrays, and tuples. "
            "The constructor type list must exactly match the original Solidity constructor signature."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "creation_input": {
                    "type": "string",
                    "description": (
                        "Full contract creation bytecode (deployment transaction input), "
                        "with or without the 0x prefix."
                    ),
                },
                "constructor_types": {
                    "type": "string",
                    "description": (
                        "Comma-separated Solidity constructor parameter types, e.g. "
                        '"bytes32", "address,uint256", or "address,uint256,bool".'
                    ),
                },
            },
            "required": ["creation_input", "constructor_types"],
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
            "Run an arbitrary shell command for anything the other tools don't cover.\n\n"
            "Available utilities:\n"
            "  rg <pattern> [path]     — fast ripgrep\n"
            "  fd <pattern> [path]     — fast find\n"
            "  jq <filter>             — JSON processing\n"
            "  cloc [path]             — count lines of code\n"
            "  tree [path]             — directory structure\n"
            "  xxd <file>              — hex dump\n"
            "  strings <file>          — extract printable strings\n"
            "  solhint [path]          — Solidity linter\n"
            "  node -e '<js>'          — quick JS evaluation\n"
            "  solc-select use <ver>   — switch compiler version"
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
        "description": "Search the web for security-relevant information you don't have locally.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "3-6 word query."},
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
            "snippet isn't enough.\n\n"
            "PDFs are auto-detected and extracted via pdftotext automatically — no need to "
            "call read_document separately if you already have a direct PDF or any other document type URL.\n\n"
            "If a page returns very little text, it's likely a JavaScript-rendered app whose "
            "content isn't in the static HTML — don't keep re-fetching it; pivot to browser_fethc, web_search "
            "or github_search instead."
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
    {
        "name": "browser_fetch",
        "description": (
            "Fetch a URL with a real headless Chromium browser instead of a raw HTTP request. Use "
            "this when web_fetch reports very little extracted text (JS-rendered single-page app), "
            "or to drive a search engine whose results only render client-side, e.g. "
            "'https://search.brave.com/search?q=<query>&source=web'.\n\n"
            "Slower than web_fetch (full browser boot, ~2-5s) — try web_fetch first for ordinary "
            "pages and only reach for this when it's actually needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Exact URL to render and extract text from.",
                },
                "max_chars": {"type": "integer", "default": 8000},
                "wait_ms": {
                    "type": "integer",
                    "default": 0,
                    "description": "Extra wait after page load, ms — only needed if content finishes rendering after network-idle.",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "download_file",
        "description": (
            "Download a binary file (PDF, zip, etc.) to local scratch space and return its "
            "saved path. Use this when you need the raw file for further processing rather "
            "than just its extracted text — e.g. before running shell tools on it. For PDFs "
            "you want to read, read_document is usually simpler (it downloads automatically)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Direct URL to the file.",
                },
                "filename": {
                    "type": "string",
                    "description": "Optional filename override; inferred from the URL if omitted.",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "read_document",
        "description": (
            "Extract text from a document. Audit reports and findings trackers come in several "
            "formats: PDF (Trail of Bits, Spearbit, OpenZeppelin, Code4rena, Sherlock all publish "
            "these), Word .docx, and Excel .xlsx/.xlsm or .csv/.tsv finding trackers.\n\n"
            "Pass either a direct URL (downloaded automatically) or a local path already returned "
            "by download_file. Scanned/image-only PDFs cannot be read (no OCR support) — look for "
            "an HTML version instead if extraction fails."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "A URL, or a local path from download_file. Supported: .pdf, .docx, .xlsx, .xlsm, .csv, .tsv.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Max characters of extracted text to return.",
                    "default": 15000,
                },
            },
            "required": ["source"],
        },
    },
    {
        "name": "github_search",
        "description": (
            "Search GitHub repositories or code via the public REST API. No API key needed "
            "(unauthenticated, rate-limited to ~10 search req/min). Use this to find:\n"
            "  - audit publication repos: trailofbits/publications, spearbit/portfolio, "
            "code-423n4/* (Code4rena), sherlock-audit/*\n"
            "  - a protocol's own source repo when you only have its name\n"
            "  - code search across GitHub for specific function signatures or patterns\n"
            "Prefer this over web_search when you specifically want GitHub results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query, e.g. 'org:trailofbits-publications <protocol name>'.",
                },
                "search_type": {
                    "type": "string",
                    "enum": ["repositories", "code"],
                    "default": "repositories",
                },
                "max_results": {
                    "type": "integer",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["query"],
        },
    },
]

DISPATCHABLE_TOOL_NAMES: frozenset[str] = frozenset(spec["name"] for spec in TOOL_SPECS)


_STDOUT_LIMITS: dict[str, int] = {
    "slither": 200_000,
    "aderyn": 200_000,
}
_DEFAULT_STDOUT_LIMIT = 10_000


def dispatch(
    tool_name: str,
    tool_input: dict,
    project_path: str,
    repo_graph: Graph,
    reporter: Reporter | None = None,
    search_state: SearchState | None = None,
    debug_logger: DebugLogger | None = None,
) -> ToolResult:
    debug_logger = debug_logger or default_debug_logger

    owns_window = reporter is None
    window = None
    if owns_window:
        window = RollingWindow()
        window.__enter__()
        reporter = RichReporter(window)

    args_repr = ", ".join(f"{k}={v!r}" for k, v in tool_input.items())
    cmd_display = _tool_display(tool_name, tool_input)

    try:
        reporter.tool_started(tool_name, cmd_display)

        t0 = time.monotonic()

        match tool_name:
            case "graph_query":
                node = tool_input.get("node", "")
                relation = tool_input.get("relation", "")
                method = {
                    "callers": repo_graph.callers,
                    "callees": repo_graph.callees,
                    "inherits": repo_graph.inherits,
                    "inherited_by": repo_graph.inherited_by,
                    "dead_code": lambda _n: repo_graph.dead_code(),
                    "search": lambda n: repo_graph.search(n),
                }.get(relation)
                if method is None:
                    result = ToolResult(
                        "graph_query", "", f"Unknown relation '{relation}'.", 1
                    )
                else:
                    result = ToolResult(
                        "graph_query", json.dumps(method(node)), "", 0
                    )
            case "slither":
                result = slither(
                    project_path, tool_input.get("flags") or "./", reporter=reporter
                )
            case "aderyn":
                result = aderyn(
                    project_path, tool_input.get("flags") or "", reporter=reporter
                )
            case "forge":
                result = forge(
                    project_path,
                    tool_input.get("subcommand") or "build",
                    reporter=reporter,
                )
            case "cast":
                subcommand = tool_input.get("subcommand", "")
                result = (
                    cast(project_path, subcommand, reporter=reporter)
                    if subcommand
                    else ToolResult("cast", "", "Missing required 'subcommand'.", 1)
                )
            case "evm_decode_constructor":
                result = evm_decode_constructor(
                    project_path,
                    tool_input.get("creation_input", ""),
                    tool_input.get("constructor_types", ""),
                    reporter=reporter,
                )
            case "git":
                result = git(
                    project_path,
                    tool_input.get("args") or "log --oneline -10",
                    reporter=reporter,
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
                    shell(project_path, command, reporter=reporter)
                    if command
                    else ToolResult("shell", "", "Missing required 'command'.", 1)
                )
            case "web_search":
                query = tool_input.get("query", "")
                max_results = tool_input.get("max_results", 5)
                with reporter.spinner("Searching…"):
                    result = (
                        search_state.consume(
                            "web_search", query, lambda: web_search(query, max_results)
                        )
                        if search_state is not None
                        else web_search(query, max_results)
                    )
            case "web_fetch":
                url = tool_input.get("url", "")
                with reporter.spinner("Fetching…"):
                    result = (
                        search_state.consume("web_fetch", url, lambda: web_fetch(url))
                        if search_state is not None
                        else web_fetch(url)
                    )
            case "browser_fetch":
                url = tool_input.get("url", "")
                max_chars = tool_input.get("max_chars", 8_000)
                wait_ms = tool_input.get("wait_ms", 0)
                with reporter.spinner("Rendering page…"):
                    result = (
                        search_state.consume(
                            "browser_fetch",
                            url,
                            lambda: browser_fetch(url, max_chars, wait_ms),
                        )
                        if search_state is not None
                        else browser_fetch(url, max_chars, wait_ms)
                    )
            case "download_file":
                url = tool_input.get("url", "")
                filename = tool_input.get("filename")
                with reporter.spinner("Downloading…"):
                    result = (
                        download_file(url, filename)
                        if url
                        else ToolResult(
                            "download_file", "", "Missing required 'url'.", 1
                        )
                    )
            case "read_document":
                source = tool_input.get("source", "")
                max_chars = tool_input.get("max_chars", 15_000)
                with reporter.spinner("Reading document…"):
                    result = (
                        read_document(source, max_chars)
                        if source
                        else ToolResult(
                            "read_document", "", "Missing required 'source'.", 1
                        )
                    )
            case "github_search":
                query = tool_input.get("query", "")
                search_type = tool_input.get("search_type", "repositories")
                max_results = tool_input.get("max_results", 5)
                with reporter.spinner("Searching GitHub…"):
                    result = (
                        github_search(query, search_type, max_results)
                        if query
                        else ToolResult(
                            "github_search", "", "Missing required 'query'.", 1
                        )
                    )
            case _:
                result = ToolResult(
                    tool_name,
                    "",
                    f"Unknown tool '{tool_name}'. Available: {sorted(DISPATCHABLE_TOOL_NAMES)}",
                    1,
                )

        elapsed = time.monotonic() - t0

        stdout_limit = _STDOUT_LIMITS.get(tool_name, _DEFAULT_STDOUT_LIMIT)
        smart_stdout = _smart_process_output(
            result.stdout, tool_name, project_path, stdout_limit
        )
        smart_stderr = _truncate(
            result.stderr,
            4_000,
            "Use a more targeted command (narrower flags, grep, head/tail) to see more.",
        )

        result = ToolResult(
            result.command,
            smart_stdout,
            smart_stderr,
            result.returncode,
            is_truncation_system_prompt=True,
        )

        debug_logger.log(tool_name, args_repr, result)
        reporter.tool_finished(result, elapsed)

    finally:
        if owns_window:
            window.__exit__(None, None, None)

    return result
