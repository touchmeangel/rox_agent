from __future__ import annotations

from dataclasses import dataclass, field

from ignite_agent.debug_logger import DebugLogger
from ignite_agent.rolling_window import RollingWindow
from ignite_agent.search_state import SearchState
from ignite_agent.steps.base import BaseStep, StepResult
from ignite_agent.tools import dispatch


@dataclass
class ProtocolTarget:
    name: str
    purpose: str
    version: str
    is_fork_upstream: bool
    prefetch_results: list[str] = field(default_factory=list)


def _build_targets(context: dict) -> list[ProtocolTarget]:
    doc_findings = context.get("documentation", {}).get("findings", {})
    fork_findings = context.get("fork_analysis", {}).get("findings", {})

    targets: list[ProtocolTarget] = []

    if fork_findings.get("is_fork") and fork_findings.get("upstream"):
        upstream = fork_findings["upstream"]
        name = upstream.get("protocol", "").strip()
        if name:
            targets.append(
                ProtocolTarget(
                    name=name,
                    purpose=f"Fork upstream (base commit {upstream.get('base_commit', '?')})",
                    version="",
                    is_fork_upstream=True,
                )
            )

    for p in doc_findings.get("external_protocols", []):
        name = p.get("name", "").strip()
        if name:
            targets.append(
                ProtocolTarget(
                    name=name,
                    purpose=p.get("purpose", ""),
                    version=p.get("version", ""),
                    is_fork_upstream=False,
                )
            )

    return targets


def _prefetch_protocol(
    target: ProtocolTarget,
    project_path: str,
    window: RollingWindow,
    debug_logger: DebugLogger,
    search_state: SearchState,
    max_chars_per_result: int = 1_500,
) -> None:
    queries = [
        f"{target.name} audit findings solodit",
        f"{target.name} vulnerability audit report",
    ]
    for query in queries:
        result = dispatch(
            "web_search",
            {"query": query},
            project_path,
            window=window,
            search_state=search_state,
            debug_logger=debug_logger,
        )
        out = (result.stdout or "").strip()
        if out:
            target.prefetch_results.append(
                f"Query: {query}\n{out[:max_chars_per_result]}"
            )


def _fmt_target(target: ProtocolTarget) -> str:
    tag = " [FORK UPSTREAM]" if target.is_fork_upstream else ""
    lines = [
        f"Protocol: {target.name}{tag}",
        f"Version:  {target.version or 'unknown'}",
        f"Usage:    {target.purpose}",
    ]
    if target.prefetch_results:
        lines.append("\nPre-fetched search results:")
        for r in target.prefetch_results:
            lines.append(f"\n{r}")
    else:
        lines.append("\nPre-fetch: (no results returned)")
    return "\n".join(lines)


class KnownIssuesStep(BaseStep):
    name = "known_issues"

    def select_context(self, global_context: dict) -> dict:
        return {
            "documentation": global_context.get("documentation", {}),
            "fork_analysis": global_context.get("fork_analysis", {}),
        }

    def run(self, context: dict) -> StepResult:
        targets = _build_targets(context)
        if not targets:
            return StepResult(
                step_name=self.name,
                success=True,
                findings={
                    "protocol_checklists": [],
                    "summary": "No external protocols or fork upstream identified — step skipped.",
                },
                summary="No external protocols or fork upstream identified — step skipped.",
                iterations_used=0,
            )
        return super().run(context)

    def findings_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "protocol_checklists": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "protocol": {"type": "string"},
                            "version": {"type": "string"},
                            "known_issues": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "source": {"type": "string"},
                                        "title": {"type": "string"},
                                        "description": {"type": "string"},
                                        "severity": {
                                            "type": "string",
                                            "enum": [
                                                "critical",
                                                "high",
                                                "medium",
                                                "low",
                                                "informational",
                                            ],
                                        },
                                        "check": {"type": "string"},
                                        "applies_if": {"type": "string"},
                                    },
                                    "required": [
                                        "id",
                                        "source",
                                        "title",
                                        "description",
                                        "severity",
                                        "check",
                                    ],
                                },
                            },
                        },
                        "required": ["protocol", "known_issues"],
                    },
                },
                "summary": {"type": "string"},
            },
            "required": ["protocol_checklists", "summary"],
        }

    def validate_complete(self, findings: dict) -> str | None:
        if not isinstance(findings, dict):
            return "the output should be json object (python dict)"
            
        if not findings.get("summary"):
            return "summary is not set which is required"
            
        checklists = findings.get("protocol_checklists")
        if not isinstance(checklists, list):
            return "invalid protocol_checklists, must be a defined list"
            
        return None

    def initial_prompt(
        self, context: dict, window: RollingWindow, debug_logger: DebugLogger
    ) -> str:
        doc_findings = context.get("documentation", {}).get("findings", {})
        protocol_summary = doc_findings.get("protocol_summary", "(none)")

        targets = _build_targets(context)

        search_state = SearchState(max_calls=len(targets) * 2)
        for t in targets:
            _prefetch_protocol(t, self.project_path, window, debug_logger, search_state)

        targets_section = "\n\n────────────────────────────────────\n\n".join(
            _fmt_target(t) for t in targets
        )
        prefetch_note = (
            f"Pre-fetched {sum(len(t.prefetch_results) for t in targets)} "
            f"search results across {len(targets)} protocol(s). "
            "Use web_fetch on any URLs that look promising."
        )

        return f"""You are running the known-issues research step of a security audit.
Project: {self.project_path}

════════════════════════════════════════
PROTOCOL CONTEXT
════════════════════════════════════════

What this protocol does:
{protocol_summary}

════════════════════════════════════════
RESEARCH TARGETS  ({prefetch_note})
════════════════════════════════════════

{targets_section}

════════════════════════════════════════
YOUR TASK
════════════════════════════════════════

For each protocol listed above, find every known audit finding or disclosed
vulnerability that is relevant to HOW that protocol is used here.
The "Usage" field tells you the integration context — use it to decide
which findings actually apply.

This step does NOT read the project source. It only researches external sources.
The audit step will verify each checklist item against the code.

STEP 1 — EXTRACT FROM PRE-FETCHED RESULTS
  For any URL in the results above that looks like an audit report or
  finding list, call web_fetch to read the full content.
  Prioritise: Cantina, Trail of Bits, OpenZeppelin, Spearbit, Code4rena,
  Sherlock, Immunefi, and the protocol's own GitHub /audits folder.

STEP 2 — FILL GAPS WITH ADDITIONAL SEARCHES
  If pre-fetched results are thin for a protocol, run at most 1 additional
  targeted search per protocol:
    web_search: "<protocol> <version> audit cantina OR spearbit OR trail-of-bits"
  Once you have findings from a credible source, stop searching that protocol.
  If nothing surfaces after the additional search, record empty known_issues
  and move on — do not keep rephrasing the same query.

STEP 3 — FILTER BY USAGE CONSECTION
  For each finding ask: does the Usage description create the conditions
  for this vulnerability?
    • "Used for swapping tokens" → share inflation from market creation does NOT apply
    • "Creates new lending markets" → share inflation DOES apply
  Exclude findings that cannot apply — do not include them with a caveat.

STEP 4 — WRITE ACTIONABLE CHECKS
  Each finding needs a concrete check — not "review X" but
  "verify that function Y enforces invariant Z before calling into protocol".

════════════════════════════════════════
FINDINGS FORMAT
════════════════════════════════════════

{{
  "protocol_checklists": [
    {{
      "protocol": "Morpho Blue",
      "version": "v1.0",
      "known_issues": [
        {{
          "id": "KI-001",
          "source": "Cantina audit, finding 3.1.4",
          "title": "First-borrower share inflation",
          "description": "The first borrower in a fresh market can skew totalBorrowShares because virtual amounts reduce but do not eliminate the risk.",
          "severity": "high",
          "check": "Verify that new markets are seeded at creation or the integration explicitly handles the share inflation edge case.",
          "applies_if": "Protocol creates or interacts with fresh Morpho markets"
        }}
      ]
    }}
  ],
  "summary": "Researched 3 protocols. Found 7 relevant known issues: 2 high, 3 medium, 2 low."
}}

════════════════════════════════════════
RULES
════════════════════════════════════════

• Maximum 1 additional search per protocol beyond the pre-fetch.
• Source must be specific: firm + report name + section, not just a URL.
• check must be executable: a concrete thing to verify in code.
• If a protocol has no relevant public findings, record empty known_issues.
• Do not read project source files — that is the audit step's job.
"""
