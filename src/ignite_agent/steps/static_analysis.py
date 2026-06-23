from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ignite_agent.debug_logger import DebugLogger
from ignite_agent.rolling_window import RollingWindow
from ignite_agent.steps.base import BaseStep
from ignite_agent.tools import dispatch

# Inline content in the initial prompt when the project is small enough.
_INLINE_LINE_LIMIT: int = 8_000

_IMPACT_ORDER = ["High", "Medium", "Low", "Informational", "Optimization"]

_EXCLUDE_DIRS = frozenset(
    {
        "lib",
        "test",
        "tests",
        "out",
        "cache",
        "artifacts",
        "build",
    }
)


@dataclass
class PreScanResult:
    tool_name: str
    success: bool  # True when the tool ran and its own JSON reports success
    detectors: list[dict] = field(default_factory=list)
    error: str | None = None
    stderr_tail: str | None = None
    exit_code: int | None = None


def _run_prescan_tool(
    project_path: str,
    tool_name: str,
    flags: str,
    parse_fn: Callable[[str, str, int], PreScanResult],
    window: RollingWindow,
    debug_logger: DebugLogger,
) -> PreScanResult:
    result = dispatch(
        tool_name,
        {"flags": flags},
        project_path,
        window=window,
        search_state=None,
        debug_logger=debug_logger,
    )

    if result.returncode == 127 or "command not found" in result.stderr.lower():
        return PreScanResult(
            tool_name=tool_name,
            success=False,
            error=f"'{tool_name}' binary not found in PATH",
            exit_code=result.returncode,
        )

    return parse_fn(result.stdout, result.stderr, result.returncode)


_SLITHER_FLAGS = (
    f". --json - --exclude-dependencies "
    f"--filter-paths 'test,tests,out,cache,artifacts,build' "
    f"--exclude naming-convention,solc-version,low-level-calls,pragma,similar-names"
)


def _parse_slither(stdout: str, stderr: str, exit_code: int) -> PreScanResult:
    raw = stdout.strip()
    if not raw:
        return PreScanResult(
            tool_name="slither",
            success=False,
            error="slither produced no output",
            stderr_tail=stderr[-1_000:].strip() or None,
            exit_code=exit_code,
        )
    try:
        data = json.loads(raw)
        return PreScanResult(
            tool_name="slither",
            success=bool(data.get("success")),
            detectors=data.get("results", {}).get("detectors", []),
            error=data.get("error"),
            stderr_tail=stderr[-1_000:].strip() or None,
            exit_code=exit_code,
        )
    except json.JSONDecodeError:
        return PreScanResult(
            tool_name="slither",
            success=False,
            error=f"JSON parse error — stdout tail: {raw[-600:]}",
            stderr_tail=stderr[-1_000:].strip() or None,
            exit_code=exit_code,
        )


def _run_slither(
    project_path: str,
    window: RollingWindow,
    debug_logger: DebugLogger,
) -> PreScanResult:
    return _run_prescan_tool(
        project_path,
        "slither",
        _SLITHER_FLAGS,
        _parse_slither,
        window=window,
        debug_logger=debug_logger,
    )


def _collect_sol_files(project_path: str) -> tuple[list[dict], int, bool]:
    path = Path(project_path)
    sol_files: list[dict] = []

    for f in sorted(path.rglob("*.sol")):
        # Drop any file whose path passes through an excluded directory.
        if _EXCLUDE_DIRS.intersection(f.relative_to(path).parts[:-1]):
            continue
        rel = str(f.relative_to(path))
        try:
            content = f.read_text(errors="replace")
            sol_files.append(
                {
                    "path": rel,
                    "lines": len(content.splitlines()),
                    "content": content,
                }
            )
        except Exception as exc:
            sol_files.append(
                {"path": rel, "lines": -1, "content": None, "error": str(exc)}
            )

    total_lines = sum(f["lines"] for f in sol_files if f.get("lines", -1) >= 0)
    inlined = bool(sol_files) and total_lines <= _INLINE_LINE_LIMIT

    if not inlined:
        for f in sol_files:
            f.pop("content", None)

    return sol_files, total_lines, inlined


def _pre_scan_project(
    project_path: str, window: RollingWindow, debug_logger: DebugLogger
) -> dict:
    sol_files, total_lines, inlined = _collect_sol_files(project_path)
    slither = _run_slither(project_path, window, debug_logger)
    return {
        "sol_files": sol_files,
        "slither": slither,
        "total_lines": total_lines,
        "inlined": inlined,
    }


def _fmt_slither(result: PreScanResult) -> str:
    detectors = result.detectors

    if not result.success:
        lines = [f"⚠  Slither exited with an error (exit {result.exit_code or '?'})."]
        if result.error:
            lines.append(f"   Error: {result.error}")
        if result.stderr_tail:
            lines.append(f"   Stderr:\n{result.stderr_tail}")
        if detectors:
            lines.append(
                f"\n   Despite the error, {len(detectors)} detector result(s) were returned "
                "and are shown below. They may be partial."
            )
            lines.append("")
            lines.append(_fmt_detectors(detectors))
        else:
            lines.append("   No detector results — proceed with manual scanning.")
        return "\n".join(lines)

    if not detectors:
        return "✓  Slither ran successfully — zero detector findings."

    return f"✓  Slither: {len(detectors)} finding(s)\n\n" + _fmt_detectors(detectors)


def _fmt_detectors(detectors: list[dict]) -> str:
    by_impact: dict[str, list[dict]] = {}
    for d in detectors:
        by_impact.setdefault(d.get("impact", "Informational"), []).append(d)

    blocks: list[str] = []
    for impact in _IMPACT_ORDER:
        group = by_impact.get(impact)
        if not group:
            continue
        blocks.append(f"── {impact.upper()} ({len(group)}) {'─' * (40 - len(impact))}")
        for d in group:
            check = d.get("check", "?")
            confidence = d.get("confidence", "?")
            desc = d.get("description", "").replace("\n", " ").strip()
            if len(desc) > 300:
                desc = desc[:300] + "…"

            locs: list[str] = []
            seen_locs: set[str] = set()
            for el in d.get("elements", []):
                sm = el.get("source_mapping", {})
                fname = sm.get("filename_short", "")
                if not fname:
                    continue
                raw_lines = sm.get("lines", [])
                if raw_lines:
                    loc = (
                        f"{fname}:{raw_lines[0]}"
                        if len(raw_lines) == 1
                        else f"{fname}:{raw_lines[0]}-{raw_lines[-1]}"
                    )
                else:
                    loc = fname
                if loc not in seen_locs:
                    seen_locs.add(loc)
                    locs.append(loc)

            loc_str = ", ".join(locs[:4])
            if len(locs) > 4:
                loc_str += f" (+{len(locs) - 4} more)"

            blocks.append(f"\n  [{check}]  confidence={confidence}")
            if loc_str:
                blocks.append(f"  → {loc_str}")
            blocks.append(f"  {desc}")

    return "\n".join(blocks)


def _fmt_sol_files(sol_files: list[dict], inlined: bool) -> str:
    if not sol_files:
        return (
            "No .sol files were found during pre-scan.\n"
            "Use list_sol_files or shell('find . -name \"*.sol\"') to locate them manually."
        )

    if not inlined:
        file_list = "\n".join(f"  {f['path']}  ({f['lines']} lines)" for f in sol_files)
        return (
            f"{len(sol_files)} Solidity file(s) found — codebase is too large to inline, "
            f"use read_file to read them:\n\n{file_list}"
        )

    sep = "─" * 64
    parts: list[str] = []
    for f in sol_files:
        header = f"{sep}\n{f['path']}  ({f['lines']} lines)\n{sep}"
        body = f.get("content") or "(read error — use read_file to load this file)"
        parts.append(f"{header}\n{body}")
    return "\n\n".join(parts)


class StaticAnalysisStep(BaseStep):
    name = "static_analysis"

    def select_context(self, global_context: dict) -> dict:
        return {}

    def initial_prompt(
        self, context: dict, window: RollingWindow, debug_logger: DebugLogger
    ) -> str:
        pre_scan = _pre_scan_project(self.project_path, window, debug_logger)
        sol_files: list[dict] = pre_scan["sol_files"]
        inlined: bool = pre_scan["inlined"]
        total_lines: int = pre_scan["total_lines"]
        slither_result: PreScanResult = pre_scan["slither"]

        slither_section = _fmt_slither(slither_result)
        files_section = _fmt_sol_files(sol_files, inlined)

        if inlined:
            files_note = "All source files are inlined above — no read_file calls needed for the files listed."
        else:
            files_note = (
                f"Source files are listed above ({total_lines:,} lines total). "
                "Start with read_file on any file referenced by a High or Medium slither finding."
            )

        if slither_result.success:
            slither_instruction = (
                "Slither has already been run — its results are above. "
                "Do NOT re-run slither unless you need a specific flag that wasn't used."
            )
        else:
            slither_instruction = (
                "Slither did not produce clean output. You may try re-running it with "
                "specific flags (e.g. '--filter-paths lib' if it failed on dependencies), "
                "but don't loop on it — if it keeps failing, pivot to manual scanning."
            )

        return f"""You are running the static analysis step of a security audit.
Project: {self.project_path}

════════════════════════════════════════
SLITHER OUTPUT (pre-run)
════════════════════════════════════════

{slither_section}

{slither_instruction}

════════════════════════════════════════
SOURCE FILES
════════════════════════════════════════

{files_section}

{files_note}

════════════════════════════════════════
YOUR TASK
════════════════════════════════════════

TRIAGE STATIC SCAN FINDINGS
   For each Critical, High and Medium finding above:
   • Read the flagged code if it is not already inlined.
   • Decide: CONFIRMED, SUSPECTED, or FALSE_POSITIVE.
   • Write a short evidence note for your decision.
   Low / Informational / Optimization findings go straight to the
   informational bucket unless something looks genuinely exploitable.

════════════════════════════════════════
FINDINGS FORMAT  (pass this to submit_findings)
════════════════════════════════════════

{{
  "confirmed": [
    {{
      "id": "SA-001",
      "origin": "slither | manual",
      "check": "reentrancy-eth",
      "severity": "critical | high | medium | low",
      "title": "Short title",
      "location": {{
        "file": "src/Vault.sol",
        "function": "withdraw",
        "lines": "45-67"
      }},
      "description": "What the vulnerability is and why it is exploitable",
      "evidence": "// paste the key code snippet or the slither description",
      "impact": "What an attacker can achieve and the blast radius",
      "recommendation": "Concrete fix (omit if not obvious)"
    }}
  ],

  "suspected": [
    {{
      "id": "SA-S01",
      "origin": "slither | manual",
      "check": "optional",
      "title": "What looks suspicious",
      "location": {{
        "file": "src/Oracle.sol",
        "function": "getPrice",
        "lines": "23-30"
      }},
      "why_uncertain": "What you would need to confirm to be sure this is exploitable",
      "what_to_check": "Specific question for the human reviewer",
      "estimated_severity": "high | medium | low"
    }}
  ],

  "false_positives": [
    {{
      "check": "constable-states",
      "location": "src/CoinFlip.sol:7",
      "reason": "One sentence: why this is not a real issue"
    }}
  ],

  "informational": [
    {{
      "id": "SA-N01",
      "check": "constable-states",
      "title": "Short title",
      "location": "src/CoinFlip.sol:7",
      "description": "Gas / style note — no security impact"
    }}
  ]
}}

════════════════════════════════════════
RULES
════════════════════════════════════════

• CONFIRMED requires reading the code and tracing the exploit path yourself.
  If you spotted the pattern but haven't verified it is actually exploitable, use SUSPECTED.
• SUSPECTED is not a catch-all for uncertainty — use it only when you have a
  concrete reason to think an issue exists but lack the evidence to be certain.
• FALSE_POSITIVE needs one clear, factual sentence explaining the dismissal.
• Every confirmed or suspected finding must have a location with file and function.
• Do not invent findings without code evidence.
"""

    def validate_complete(self, findings: dict) -> bool:
        if not isinstance(findings, dict):
            return False
        required = {"confirmed", "suspected", "false_positives", "informational"}
        return required.issubset(findings.keys())
