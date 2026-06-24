from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ignite_agent.debug_logger import DebugLogger
from ignite_agent.rolling_window import RollingWindow
from ignite_agent.steps.base import BaseStep
from ignite_agent.tools import dispatch

_IMPACT_ORDER = ["High", "Medium", "Low", "Informational", "Optimization"]

_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "test",
        "tests",
        "lib",
        "mock",
        "mocks",
        "out",
        "cache",
        "artifacts",
        "build",
    }
)

_SLITHER_FILTER = ",".join(sorted(_SKIP_DIRS))


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
    ". --json - --exclude-dependencies "
    f"--filter-paths '{_SLITHER_FILTER}' "
    "--exclude naming-convention,solc-version,low-level-calls,pragma,similar-names"
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


_ADERYN_FLAGS = "-o {report_path}"


def _run_aderyn(
    project_path: str,
    window: RollingWindow,
    debug_logger: DebugLogger,
) -> PreScanResult:
    report_path = Path(project_path) / "aderyn_report.json"

    def parse_aderyn_file(stdout: str, stderr: str, exit_code: int) -> PreScanResult:
        if not report_path.exists():
            return PreScanResult(
                tool_name="aderyn",
                success=False,
                error=f"Aderyn finished (exit {exit_code}), but no JSON report was generated.",
                stderr_tail=stderr[-1_000:].strip() or None,
                exit_code=exit_code,
            )

        try:
            with open(report_path, encoding="utf-8") as f:
                data = json.load(f)

            normalized_detectors = []

            severity_mappings = {
                "critical_issues": "High",
                "high_issues": "High",
                "medium_issues": "Medium",
                "low_issues": "Low",
                "nc_issues": "Informational",
            }

            for bucket_key, target_severity in severity_mappings.items():
                bucket = data.get(bucket_key, {})
                raw_issues = (
                    bucket.get("issues", []) if isinstance(bucket, dict) else []
                )

                for issue in raw_issues:
                    normalized_detectors.append(
                        {
                            "check": issue.get("title", "Unknown Detector"),
                            "impact": target_severity,
                            "confidence": "High",
                            "description": issue.get("description", ""),
                            "elements": issue.get("instances", []),
                        }
                    )

            return PreScanResult(
                tool_name="aderyn",
                success=True,
                detectors=normalized_detectors,
                error=None,
                stderr_tail=stderr[-1_000:].strip() or None,
                exit_code=exit_code,
            )
        except json.JSONDecodeError:
            return PreScanResult(
                tool_name="aderyn",
                success=False,
                error="Failed to parse the generated aderyn_report.json file.",
                stderr_tail=stderr[-1_000:].strip() or None,
                exit_code=exit_code,
            )
        finally:
            if report_path.exists():
                try:
                    report_path.unlink()
                except Exception:
                    pass

    return _run_prescan_tool(
        project_path,
        "aderyn",
        _ADERYN_FLAGS.format(report_path=str(report_path)),
        parse_aderyn_file,
        window=window,
        debug_logger=debug_logger,
    )


_SWC_SEVERITY: dict[str, str] = {
    "100": "High",
    "101": "High",
    "104": "High",
    "105": "High",
    "106": "High",
    "107": "High",
    "110": "High",
    "112": "High",
    "113": "High",
    "115": "High",
    "116": "High",
    "124": "High",
    "103": "Medium",
    "108": "Medium",
    "109": "Medium",
    "111": "Medium",
    "114": "Medium",
    "120": "Medium",
    "122": "Medium",
    "123": "Medium",
    "128": "Medium",
    "131": "Medium",
    "132": "Medium",
    "102": "Low",
    "117": "Low",
    "118": "Low",
    "119": "Low",
    "121": "Low",
    "125": "Low",
    "126": "Low",
    "129": "Low",
}


def _parse_mythril(stdout: str, stderr: str, exit_code: int) -> PreScanResult:
    raw = stdout.strip()
    if not raw:
        return PreScanResult(
            tool_name="mythril",
            success=False,
            error="mythril produced no output",
            stderr_tail=stderr[-1_000:].strip() or None,
            exit_code=exit_code,
        )
    try:
        data = json.loads(raw)
        detectors = []
        for issue in data.get("issues", []):
            swc_id = str(issue.get("swc-id", ""))
            severity = _SWC_SEVERITY.get(swc_id, issue.get("severity", "Medium"))
            filename = issue.get("filename", "")
            lineno = issue.get("lineno")
            elements = []
            if filename:
                elements.append(
                    {
                        "source_mapping": {
                            "filename_short": Path(filename).name,
                            "lines": [lineno] if lineno else [],
                        }
                    }
                )
            title = issue.get("title", "")
            desc = issue.get("description", "")
            detectors.append(
                {
                    "check": f"SWC-{swc_id}" if swc_id else "mythril-finding",
                    "impact": severity,
                    "confidence": "Medium",  # mythril has its own false positives
                    "description": f"{title}: {desc}".strip(": "),
                    "elements": elements,
                }
            )
        return PreScanResult(
            tool_name="mythril",
            success=True,
            detectors=detectors,
            error=data.get("error"),
            stderr_tail=stderr[-1_000:].strip() or None,
            exit_code=exit_code,
        )
    except json.JSONDecodeError:
        return PreScanResult(
            tool_name="mythril",
            success=False,
            error=f"JSON parse error — stdout tail: {raw[-600:]}",
            stderr_tail=stderr[-1_000:].strip() or None,
            exit_code=exit_code,
        )


def _run_mythril(
    project_path: str,
    window: RollingWindow,
    debug_logger: DebugLogger,
) -> PreScanResult:
    src_dir = Path(project_path) / "src"
    if not src_dir.exists():
        src_dir = Path(project_path)

    sol_files = [
        str(p)
        for p in src_dir.rglob("*.sol")
        if not any(part in _SKIP_DIRS for part in p.parts)
    ]

    if not sol_files:
        return PreScanResult(
            tool_name="mythril",
            success=False,
            error="No Solidity source files found to analyze",
        )

    sol_files = sol_files[:15]
    flags = "analyze " + " ".join(sol_files) + " --output json --max-depth 12"

    return _run_prescan_tool(
        project_path,
        "myth",
        flags,
        _parse_mythril,
        window=window,
        debug_logger=debug_logger,
    )


def _fmt_mythril(result: PreScanResult) -> str:
    detectors = result.detectors
    if not result.success:
        lines = [f"⚠  Mythril exited with an error (exit {result.exit_code or '?'})."]
        if result.error:
            lines.append(f"   Error: {result.error}")
        if result.stderr_tail:
            lines.append(f"   Stderr:\n{result.stderr_tail}")
        lines.append(
            "   No detector results." if not detectors else _fmt_detectors(detectors)
        )
        return "\n".join(lines)
    if not detectors:
        return "✓  Mythril ran successfully — zero issues found."
    return f"✓  Mythril: {len(detectors)} finding(s)\n\n" + _fmt_detectors(detectors)


_4ANALY3ER_PREFIX_SEVERITY: dict[str, str] = {
    "GAS": "Optimization",
    "G": "Optimization",
    "L": "Low",
    "NC": "Informational",
    "N": "Informational",
    "M": "Medium",
    "H": "High",
}

_4ANALY3ER_HEADING = re.compile(r"^###\s+\[([A-Z]+-\d+)\]\s+(.+)$", re.MULTILINE)


def _parse_4naly3er(stdout: str, stderr: str, exit_code: int) -> PreScanResult:
    raw = stdout.strip()
    if not raw:
        return PreScanResult(
            tool_name="4naly3er",
            success=False,
            error="4naly3er produced no output",
            stderr_tail=stderr[-1_000:].strip() or None,
            exit_code=exit_code,
        )

    matches = list(_4ANALY3ER_HEADING.finditer(raw))
    if not matches:
        return PreScanResult(
            tool_name="4naly3er",
            success=False,
            error="Could not parse any findings from 4naly3er Markdown output",
            stderr_tail=stderr[-1_000:].strip() or None,
            exit_code=exit_code,
        )

    detectors = []
    for i, match in enumerate(matches):
        code = match.group(1)
        title = match.group(2).strip()

        prefix_match = re.match(r"([A-Z]+)", code)
        prefix = prefix_match.group(1) if prefix_match else "NC"
        severity = _4ANALY3ER_PREFIX_SEVERITY.get(prefix, "Informational")

        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        body = raw[body_start:body_end].strip()

        desc_match = re.match(r"^([^\n*<][^\n]*(?:\n(?![*<\n])[^\n]*)*)", body)
        description = desc_match.group(1).strip() if desc_match else title
        if len(description) > 300:
            description = description[:300] + "…"

        file_refs = re.findall(r"File:\s*([^\n]+)", body)
        elements = [
            {
                "source_mapping": {
                    "filename_short": Path(ref.strip()).name,
                    "lines": [],
                }
            }
            for ref in file_refs[:5]
        ]

        detectors.append(
            {
                "check": code,
                "impact": severity,
                "confidence": "High",
                "description": description,
                "elements": elements,
            }
        )

    return PreScanResult(
        tool_name="4naly3er",
        success=True,
        detectors=detectors,
        stderr_tail=stderr[-1_000:].strip() or None,
        exit_code=exit_code,
    )


def _run_4naly3er(
    project_path: str,
    window: RollingWindow,
    debug_logger: DebugLogger,
) -> PreScanResult:
    src_dir = Path(project_path) / "src"
    if not src_dir.exists():
        src_dir = Path(project_path)

    sol_files = [
        str(p.relative_to(project_path))
        for p in src_dir.rglob("*.sol")
        if not any(part in _SKIP_DIRS for part in p.parts)
    ]

    if not sol_files:
        return PreScanResult(
            tool_name="4naly3er",
            success=False,
            error="No Solidity source files found for 4naly3er",
        )

    scope_path = Path(project_path) / ".4naly3er_scope.txt"
    try:
        scope_path.write_text("\n".join(sol_files))
    except OSError as e:
        return PreScanResult(
            tool_name="4naly3er",
            success=False,
            error=f"Could not write scope file: {e}",
        )

    try:
        return _run_prescan_tool(
            project_path,
            "4naly3er",
            f"{scope_path} {project_path}",
            _parse_4naly3er,
            window=window,
            debug_logger=debug_logger,
        )
    finally:
        if scope_path.exists():
            try:
                scope_path.unlink()
            except Exception:
                pass


def _fmt_4naly3er(result: PreScanResult) -> str:
    detectors = result.detectors
    if not result.success:
        lines = [f"⚠  4naly3er exited with an error (exit {result.exit_code or '?'})."]
        if result.error:
            lines.append(f"   Error: {result.error}")
        if result.stderr_tail:
            lines.append(f"   Stderr:\n{result.stderr_tail}")
        lines.append(
            "   No detector results." if not detectors else _fmt_detectors(detectors)
        )
        return "\n".join(lines)
    if not detectors:
        return "✓  4naly3er ran successfully — zero findings."
    return f"✓  4naly3er: {len(detectors)} finding(s)\n\n" + _fmt_detectors(detectors)


def _pre_scan_project(
    project_path: str, window: RollingWindow, debug_logger: DebugLogger
) -> dict:
    slither = _run_slither(project_path, window, debug_logger)
    aderyn = _run_aderyn(project_path, window, debug_logger)
    mythril = _run_mythril(project_path, window, debug_logger)
    four = _run_4naly3er(project_path, window, debug_logger)
    return {
        "slither": slither,
        "aderyn": aderyn,
        "mythril": mythril,
        "4naly3er": four,
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
            lines.append("   No detector results.")
        return "\n".join(lines)

    if not detectors:
        return "✓  Slither ran successfully — zero detector findings."

    return f"✓  Slither: {len(detectors)} finding(s)\n\n" + _fmt_detectors(detectors)


def _fmt_aderyn(result: PreScanResult) -> str:
    detectors = result.detectors

    if not result.success:
        lines = [f"⚠  Aderyn exited with an error (exit {result.exit_code or '?'})."]
        if result.error:
            lines.append(f"   Error: {result.error}")
        if result.stderr_tail:
            lines.append(f"   Stderr:\n{result.stderr_tail}")
        if detectors:
            lines.append("\n   Showing available data wrapper findings:\n")
            lines.append(_fmt_detectors(detectors))
        else:
            lines.append("   No detector results.")
        return "\n".join(lines)

    if not detectors:
        return "✓  Aderyn ran successfully — zero detector findings."

    return f"✓  Aderyn: {len(detectors)} finding(s)\n\n" + _fmt_detectors(detectors)


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
            check = d.get("check", "Unknown Detector")
            confidence = d.get("confidence", "?")
            desc = d.get("description", "").replace("\n", " ").strip()
            if len(desc) > 300:
                desc = desc[:300] + "…"

            locs: list[str] = []
            seen_locs: set[str] = set()

            for el in d.get("elements", []):
                sm = el.get("source_mapping", {})
                fname = sm.get("filename_short", "")
                raw_lines = sm.get("lines", [])

                if not fname:
                    fname = el.get("contract_path", "")
                    if fname:
                        fname = Path(fname).name
                    line = el.get("line_no")
                    raw_lines = [line] if line is not None else []

                if not fname:
                    continue

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


class StaticAnalysisStep(BaseStep):
    name = "static_analysis"

    def select_context(self, global_context: dict) -> dict:
        return {}

    def initial_prompt(
        self, context: dict, window: RollingWindow, debug_logger: DebugLogger
    ) -> str:
        pre_scan = _pre_scan_project(self.project_path, window, debug_logger)

        slither_result: PreScanResult = pre_scan["slither"]
        aderyn_result: PreScanResult = pre_scan["aderyn"]
        mythril_result: PreScanResult = pre_scan["mythril"]
        four_result: PreScanResult = pre_scan["4naly3er"]

        slither_section = _fmt_slither(slither_result)
        aderyn_section = _fmt_aderyn(aderyn_result)
        mythril_section = _fmt_mythril(mythril_result)
        four_section = _fmt_4naly3er(four_result)

        instructions = []
        if slither_result.success:
            instructions.append(
                "- Slither completed successfully. Do NOT re-run it unless needed."
            )
        else:
            instructions.append(
                "- Slither encountered errors; evaluate fallback configurations if critical details are missing."
            )

        if aderyn_result.success:
            instructions.append(
                "- Aderyn ran successfully. Combine its structural insights with Slither's findings."
            )
        else:
            instructions.append("- Aderyn context collection failed or was partial.")

        if mythril_result.success:
            instructions.append(
                "- Mythril completed symbolic execution. Prioritise its High/Medium SWC findings — they are generally lower false-positive rate than static tools."
            )
        else:
            instructions.append(
                "- Mythril did not complete successfully; treat its absence as a gap in coverage for integer/assertion/delegatecall issues."
            )

        if four_result.success:
            instructions.append(
                "- 4naly3er findings are mostly Gas/NC; escalate only if a Low finding overlaps with a Slither or Mythril finding."
            )
        else:
            instructions.append("- 4naly3er did not run or produced no output.")

        tool_instructions = "\n".join(instructions)

        return f"""You are running the static analysis step of a security audit.
Project: {self.project_path}

════════════════════════════════════════
SLITHER OUTPUT (pre-run)
════════════════════════════════════════

{slither_section}

════════════════════════════════════════
ADERYN OUTPUT (pre-run)
════════════════════════════════════════

{aderyn_section}

════════════════════════════════════════
MYTHRIL OUTPUT (pre-run)
════════════════════════════════════════

{mythril_section}

════════════════════════════════════════
4NALY3ER OUTPUT (pre-run)
════════════════════════════════════════

{four_section}

{tool_instructions}

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
      "origin": "slither | aderyn",
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
      "origin": "slither | aderyn",
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
