from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ignite_core.debug_logger import DebugLogger
from ignite_core.graph import Graph
from ignite_core.reporting import Reporter
from ignite_core.steps.codebase_scalable_base import CodebaseScalableBaseStep
from ignite_core.tools import dispatch

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


@dataclass
class PreScanResult:
    tool_name: str
    success: bool
    detectors: list[dict] = field(default_factory=list)
    error: str | None = None
    stderr_tail: str | None = None
    exit_code: int | None = None


def _run_prescan_tool(
    project_path: str,
    tool_name: str,
    flags: str,
    parse_fn: Callable[[str, str, int], PreScanResult],
    repo_graph: Graph,
    reporter: Reporter,
    debug_logger: DebugLogger,
) -> PreScanResult:
    result = dispatch(
        tool_name,
        {"flags": flags},
        project_path,
        repo_graph,
        reporter=reporter,
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


def _filter_skip_dirs(detectors: list[dict]) -> list[dict]:
    def _in_skip_dir(file_path: str) -> bool:
        return any(part in _SKIP_DIRS for part in Path(file_path).parts)

    filtered = []
    for d in detectors:
        elements = d.get("elements", [])
        if not elements:
            filtered.append(d)
            continue
        kept = []
        for el in elements:
            sm = el.get("source_mapping", {}) or {}
            fname = sm.get("filename_short") or el.get("contract_path", "")
            if fname and _in_skip_dir(fname):
                continue
            kept.append(el)
        if kept:
            filtered.append({**d, "elements": kept})
    return filtered


def _run_slither(
    project_path: str, repo_graph: Graph, reporter: Reporter, debug_logger: DebugLogger
) -> PreScanResult:
    return PreScanResult(
        tool_name="slither",
        success=True,
        detectors=_filter_skip_dirs(repo_graph.detectors),
    )


_ADERYN_FLAGS = "-o {report_path}"


def _run_aderyn(
    project_path: str, repo_graph: Graph, reporter: Reporter, debug_logger: DebugLogger
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
        repo_graph,
        reporter=reporter,
        debug_logger=debug_logger,
    )


def _fmt_result(result: PreScanResult, label: str) -> str:
    if not result.success:
        lines = [f"   {label} failed (exit {result.exit_code or '?'})."]
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
    project_path: str, repo_graph: Graph, reporter: Reporter, debug_logger: DebugLogger
) -> dict[str, PreScanResult]:
    return {
        "slither": _run_slither(project_path, repo_graph, reporter, debug_logger),
        "aderyn": _run_aderyn(project_path, repo_graph, reporter, debug_logger),
    }


class StaticAnalysisStep(CodebaseScalableBaseStep):
    name = "static_analysis"

    def select_context(self, global_context: dict) -> dict:
        return {"documentation": global_context["documentation"]}

    def findings_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "confirmed": {
                    "type": "array",
                    "description": (
                        "High-confidence findings: you read the actual flagged source and it "
                        "clearly matches a real, plausibly exploitable pattern. This is a "
                        "strong escalation candidate, not a final verified verdict — the "
                        "worker phase performs the deep, per-candidate exploit verification "
                        "(and any proof-of-concept work)."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "e.g., SA-001"},
                            "origin": {
                                "type": "string",
                                "description": "slither | aderyn",
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
                    "description": "Suspicious patterns or flaws requiring secondary verification by a worker.",
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
        self, context: dict, reporter: Reporter, debug_logger: DebugLogger
    ) -> str:
        pre_scan = _pre_scan_project(
            self.project_path, self.repo_graph, reporter, debug_logger
        )

        slither_result = pre_scan["slither"]
        aderyn_result = pre_scan["aderyn"]

        slither_section = _fmt_slither(slither_result)
        aderyn_section = _fmt_aderyn(aderyn_result)

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

        tool_instructions = "\n".join(instructions)

        return f"""<audit_step name="static_analysis">

<scope>
You are running the static analysis triage step of a security audit. Your job is to sort
raw analyzer output (Slither, Aderyn) into signal and noise — not to fully verify
exploitability or produce proof-of-concept code. "Confirmed" in this step means "I read the
flagged source and this pattern clearly holds," not "I traced and proved a working
exploit." Everything you flag here is a candidate that a worker mission will later
investigate in depth, with a narrower scope and dedicated iterations to actually verify
and, if needed, demonstrate impact.
</scope>

<project_path>{self.project_path}</project_path>

<pre_scan>
<tool name="slither">
{slither_section}
</tool>

<tool name="aderyn">
{aderyn_section}
</tool>

<notes>
{tool_instructions}
</notes>
</pre_scan>

<task>
<step id="1" title="Triage static scan findings">
For each Critical, High and Medium finding above:
  - Read the flagged code if it is not already inlined.
  - Decide: CONFIRMED, SUSPECTED, or FALSE_POSITIVE.
  - Write a short evidence note for your decision.
Low / Informational / Optimization findings go straight to the informational bucket unless
something looks genuinely exploitable, in which case treat it as SUSPECTED instead.
</step>
</task>

<output_format>
Pass this to submit_findings:

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
      "description": "What the pattern is and why it looks exploitable",
      "evidence": "// paste the key code snippet or the slither description",
      "impact": "What an attacker could plausibly achieve, in your best assessment",
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
      "what_to_check": "Specific question for the worker who investigates this next",
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
</output_format>

<rules>
<rule>CONFIRMED means you read the actual flagged code and it clearly matches the pattern the detector describes — not that you have proven it exploitable end-to-end. Full exploit-path verification is the worker phase's job.</rule>
<rule>Do not write, run, or describe any test, script, or Foundry test aimed at demonstrating reachability or exploitability of a finding in this step, even a minimal one. Reading source is enough here.</rule>
<rule>Never write, simulate, or outline exploit code, attack sequences, or proof-of-concept logic in this step, for any bucket.</rule>
<rule>"what_to_check" for SUSPECTED items is a hint for the downstream worker, not a task for you to complete now.</rule>
</rules>

</audit_step>"""

    def validate_complete(self, findings: dict) -> str | None:
        if not isinstance(findings, dict):
            return "findings must be json object (python dict)"
        required = {"confirmed", "suspected", "false_positives", "informational"}
        missing = required - findings.keys()
        if missing:
            return f"missing required keys: {', '.join(sorted(missing))}"
        return None
