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


@dataclass
class PreScanResult:
    tool_name: str
    success: bool
    detectors: list[dict] = field(default_factory=list)
    error: str | None = None
    stderr_tail: str | None = None
    exit_code: int | None = None


def _extract_json(stdout: str) -> dict | None:
    stdout = stdout.strip()
    try:
        return json.loads(stdout)
    except Exception:
        pass
    matches = re.findall(r"\{.*\}", stdout, re.DOTALL)
    for match in reversed(matches):
        try:
            return json.loads(match)
        except Exception:
            continue
    return None


def _is_hex_blob(text: str) -> bool:
    sample = text.strip()[:200]
    hex_chars = sum(1 for c in sample if c in "0123456789abcdefABCDEF")
    return len(sample) > 20 and hex_chars / max(len(sample), 1) > 0.85


def _clean_error(raw: str | None, max_chars: int = 400) -> str | None:
    if not raw:
        return None

    text = raw.strip()

    if _is_hex_blob(text):
        return (
            "Mythril output raw EVM bytecode instead of JSON. "
            "This usually means solc failed to compile the target — "
            "check solc version compatibility (`solc-select use <ver>`) "
            "or ARM64/Rosetta issues with the solc binary."
        )

    lines = [line for line in text.splitlines() if line.strip()]

    if lines and "Traceback (most recent call last)" in lines[0]:
        return lines[-1] if lines else text

    if len(text) > max_chars:
        return "…" + text[-max_chars:]

    return text


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

    if result.is_truncation_system_prompt:
        return PreScanResult(
            tool_name=tool_name,
            success=False,
            error=result.stdout,
            exit_code=result.returncode,
        )

    return parse_fn(result.stdout, result.stderr, result.returncode)


_SLITHER_FLAGS = (
    ". --json - --exclude-dependencies "
    f"--filter-paths '{_SLITHER_FILTER}' "
    "--exclude naming-convention,solc-version,low-level-calls,pragma,similar-names"
)


def _parse_slither(stdout: str, stderr: str, exit_code: int) -> PreScanResult:
    data = _extract_json(stdout)
    if not data:
        return PreScanResult(
            tool_name="slither",
            success=False,
            error="slither produced no parseable JSON output",
            stderr_tail=stderr[-1_000:].strip() or None,
            exit_code=exit_code,
        )

    return PreScanResult(
        tool_name="slither",
        success=bool(data.get("success")),
        detectors=data.get("results", {}).get("detectors", []),
        error=data.get("error"),
        stderr_tail=stderr[-1_000:].strip() or None,
        exit_code=exit_code,
    )


def _run_slither(
    project_path: str, window: RollingWindow, debug_logger: DebugLogger
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
    project_path: str, window: RollingWindow, debug_logger: DebugLogger
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


def _parse_mythril(stdout: str, stderr: str, exit_code: int) -> PreScanResult:
    if _is_hex_blob(stdout):
        return PreScanResult(
            tool_name="mythril",
            success=False,
            error=_clean_error(stdout),
            stderr_tail=stderr[-1_000:].strip() or None,
            exit_code=exit_code,
        )

    data = _extract_json(stdout)
    if not data:
        return PreScanResult(
            tool_name="mythril",
            success=False,
            error="Mythril produced no parseable JSON output.",
            stderr_tail=stderr[-1_000:].strip() or None,
            exit_code=exit_code,
        )

    if data.get("success") is False:
        return PreScanResult(
            tool_name="mythril",
            success=False,
            error=_clean_error(data.get("error") or "Mythril reported failure."),
            stderr_tail=stderr[-1_000:].strip() or None,
            exit_code=exit_code,
        )

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
                "confidence": "Medium",
                "description": f"{title}: {desc}".strip(": "),
                "elements": elements,
            }
        )

    return PreScanResult(
        tool_name="mythril",
        success=True,
        detectors=detectors,
        stderr_tail=stderr[-1_000:].strip() or None,
        exit_code=exit_code,
    )


def _run_mythril(
    project_path: str, window: RollingWindow, debug_logger: DebugLogger
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

    flags = "analyze " + " ".join(sol_files) + " -o json --max-depth 12"

    return _run_prescan_tool(
        project_path,
        "myth",
        flags,
        _parse_mythril,
        window=window,
        debug_logger=debug_logger,
    )


def _fmt_result(result: PreScanResult, label: str) -> str:
    if not result.success:
        lines = [f"⚠  {label} failed (exit {result.exit_code or '?'})."]
        if result.error:
            lines.append(f"   Error: {result.error}")
        if result.stderr_tail:
            lines.append(f"   Stderr:\n{result.stderr_tail}")
        if result.detectors:
            lines.append(
                f"\n   Despite the error, {len(result.detectors)} finding(s) were "
                "returned and are shown below. They may be partial."
            )
            lines.append(_fmt_detectors(result.detectors))
        else:
            lines.append("   No detector results.")
        return "\n".join(lines)

    if not result.detectors:
        return f"✓  {label} — zero findings."
    return f"✓  {label}: {len(result.detectors)} finding(s)\n\n" + _fmt_detectors(
        result.detectors
    )


def _fmt_slither(result: PreScanResult) -> str:
    return _fmt_result(result, "Slither")


def _fmt_aderyn(result: PreScanResult) -> str:
    return _fmt_result(result, "Aderyn")


def _fmt_mythril(result: PreScanResult) -> str:
    return _fmt_result(result, "Mythril")


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


def _pre_scan_project(
    project_path: str, window: RollingWindow, debug_logger: DebugLogger
) -> dict[str, PreScanResult]:
    return {
        "slither": _run_slither(project_path, window, debug_logger),
        "aderyn": _run_aderyn(project_path, window, debug_logger),
        "mythril": _run_mythril(project_path, window, debug_logger),
    }


class StaticAnalysisStep(BaseStep):
    name = "static_analysis"

    def select_context(self, global_context: dict) -> dict:
        return {"documentation": global_context["documentation"]}

    def findings_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "confirmed": {
                    "type": "array",
                    "description": "Vulnerabilities completely verified by tracing the exploit path in source.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "e.g., SA-001"},
                            "origin": {
                                "type": "string",
                                "description": "slither | aderyn | mythril",
                            },
                            "check": {"type": "string"},
                            "severity": {
                                "type": "string",
                                "enum": ["critical", "high", "medium", "low"],
                            },
                            "title": {"type": "string"},
                            "location": {
                                "type": "object",
                                "properties": {
                                    "file": {"type": "string"},
                                    "function": {"type": "string"},
                                    "lines": {"type": "string"},
                                },
                                "required": ["file", "function"],
                            },
                            "description": {"type": "string"},
                            "evidence": {
                                "type": "string",
                                "description": "Code snippet or exact analyzer output text.",
                            },
                            "impact": {"type": "string"},
                            "recommendation": {"type": "string"},
                        },
                        "required": [
                            "id",
                            "origin",
                            "check",
                            "severity",
                            "title",
                            "location",
                            "description",
                            "evidence",
                            "impact",
                        ],
                    },
                },
                "suspected": {
                    "type": "array",
                    "description": "Suspicious patterns or flaws requiring secondary verification or human eyeballs.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "e.g., SA-S01"},
                            "origin": {"type": "string"},
                            "check": {"type": "string"},
                            "title": {"type": "string"},
                            "location": {
                                "type": "object",
                                "properties": {
                                    "file": {"type": "string"},
                                    "function": {"type": "string"},
                                    "lines": {"type": "string"},
                                },
                                "required": ["file", "function"],
                            },
                            "why_uncertain": {"type": "string"},
                            "what_to_check": {"type": "string"},
                            "estimated_severity": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                        },
                        "required": [
                            "id",
                            "origin",
                            "title",
                            "location",
                            "why_uncertain",
                            "what_to_check",
                            "estimated_severity",
                        ],
                    },
                },
                "false_positives": {
                    "type": "array",
                    "description": "Analyzer detections determined to be incorrect or contextual non-issues.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "check": {"type": "string"},
                            "location": {
                                "type": "string",
                                "description": "File and path references string.",
                            },
                            "reason": {"type": "string"},
                        },
                        "required": ["check", "location", "reason"],
                    },
                },
                "informational": {
                    "type": "array",
                    "description": "Gas enhancements, style enhancements, and strictly non-exploitable findings.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "e.g., SA-N01"},
                            "check": {"type": "string"},
                            "title": {"type": "string"},
                            "location": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["id", "check", "title", "location", "description"],
                    },
                },
            },
            "required": ["confirmed", "suspected", "false_positives", "informational"],
        }

    def initial_prompt(
        self, context: dict, window: RollingWindow, debug_logger: DebugLogger
    ) -> str:
        pre_scan = _pre_scan_project(self.project_path, window, debug_logger)

        slither_result = pre_scan["slither"]
        aderyn_result = pre_scan["aderyn"]
        mythril_result = pre_scan["mythril"]

        slither_section = _fmt_slither(slither_result)
        aderyn_section = _fmt_aderyn(aderyn_result)
        mythril_section = _fmt_mythril(mythril_result)

        instructions = []
        if slither_result.success:
            instructions.append(
                "- Slither completed successfully. Do NOT re-run it unless needed."
            )
        else:
            instructions.append(
                "- Slither encountered errors; evaluate fallback configurations."
            )

        if aderyn_result.success:
            instructions.append(
                "- Aderyn ran successfully. Combine its structural insights with Slither's findings."
            )
        else:
            instructions.append("- Aderyn context collection failed or was partial.")

        if mythril_result.success:
            instructions.append(
                "- Mythril completed symbolic execution. Prioritise its High/Medium SWC findings — generally lower false-positive rate than static tools."
            )
        else:
            instructions.append(
                "- Mythril did not complete successfully; treat its absence as a coverage gap for integer/assertion/delegatecall issues."
            )

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
      "origin": "slither | aderyn | mythril",
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
      "origin": "slither | aderyn | mythril",
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

    def validate_complete(self, findings: dict) -> str | None:
        if not isinstance(findings, dict):
            return "findings must be a JSON object"
        for object_field in (
            "confirmed",
            "suspected",
            "false_positives",
            "informational",
            "metrics",
            "risk_rating",
            "summary",
        ):
            if object_field not in findings:
                return f"missing top-level field: '{object_field}'"
        for i, item in enumerate(findings.get("confirmed", [])):
            loc = item.get("location", {})
            for req in ("file", "function"):
                if not loc.get(req):
                    return f"confirmed[{i}].location.'{req}' is empty — use '<declaration>' for state variables"
        return None
