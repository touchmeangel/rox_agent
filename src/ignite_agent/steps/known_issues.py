from __future__ import annotations

from dataclasses import dataclass, field

from ignite_agent.debug_logger import DebugLogger
from ignite_agent.rolling_window import RollingWindow
from ignite_agent.search_state import SearchState
from ignite_agent.steps.base import BaseStep
from ignite_agent.tools import dispatch


@dataclass
class ProtocolTarget:
    name: str
    purpose: str
    version: str
    is_fork_upstream: bool
    prefetch_results: list[str] = field(default_factory=list)


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

    def validate_complete(self, findings: dict) -> bool:
        if not isinstance(findings, dict):
            return False
        if not findings.get("summary"):
            return False
        return isinstance(findings.get("protocol_checklists"), list)

    def initial_prompt(
        self, context: dict, window: RollingWindow, debug_logger: DebugLogger
    ) -> str:
        doc_findings = context.get("documentation", {}).get("findings", {})
        fork_findings = context.get("fork_analysis", {}).get("findings", {})

        protocol_summary = doc_findings.get("protocol_summary", "(none)")
        external_protocols = doc_findings.get("external_protocols", [])

        targets: list[ProtocolTarget] = []

        if fork_findings.get("is_fork") and fork_findings.get("upstream"):
            upstream = fork_findings["upstream"]
            targets.append(
                ProtocolTarget(
                    name=upstream.get("protocol", ""),
                    purpose=f"This codebase is a fork of this protocol (base commit {upstream.get('base_commit', '?')})",
                    version="",
                    is_fork_upstream=True,
                )
            )

        for p in external_protocols:
            name = p.get("name", "").strip()
            if not name:
                continue
            targets.append(
                ProtocolTarget(
                    name=name,
                    purpose=p.get("purpose", ""),
                    version=p.get("version", ""),
                    is_fork_upstream=False,
                )
            )

        if not targets:
            targets_section = "(no external protocols identified in documentation step)"
            prefetch_note = ""
        else:
            search_state = SearchState(max_calls=len(targets) * 2)
            for t in targets:
                _prefetch_protocol(
                    t, self.project_path, window, debug_logger, search_state
                )

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

For each protocol above, extract every known audit finding or disclosed
vulnerability that is relevant to HOW that protocol is used here.
The "Usage" field for each protocol tells you the integration context —
use it to decide which findings actually apply.

This step does NOT read the project source. It only researches external sources.
The audit step will verify each checklist item against the code.

STEP 1 — EXTRACT FROM PRE-FETCHED RESULTS
  The search results above are your starting point. For any URL that looks
  like an audit report or finding list, call web_fetch to read the full content.
  Prioritise: Cantina, Trail of Bits, OpenZeppelin, Spearbit, Code4rena, Sherlock,
  Immunefi, and the protocol's own GitHub /audits folder.

STEP 2 — FILL GAPS WITH ADDITIONAL SEARCHES
  If the pre-fetched results are thin for a protocol, run targeted searches:
    web_search: "<protocol> <version> audit cantina OR spearbit OR trail-of-bits"
    web_search: "<protocol> exploit disclosed OR postmortem"
  Stop searching a protocol once you have its major findings — don't over-search.

STEP 3 — FILTER BY USAGE CONTEXT
  For each finding, ask: does the Usage description above create the conditions
  for this vulnerability?
    • "Used for swapping tokens" → share inflation from market creation does NOT apply
    • "Creates new lending markets" → share inflation DOES apply
    • "Used as oracle" → price manipulation findings apply; liquidation bugs may not
  Only include findings that plausibly apply given the stated usage.

STEP 4 — WRITE ACTIONABLE CHECKS
  Each finding needs a concrete check the auditor can execute — not "review X"
  but "verify that function Y enforces invariant Z before calling into protocol".

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
          "description": "The first borrower in a fresh market can skew totalBorrowShares because virtual amounts (VIRTUAL_ASSETS=1, VIRTUAL_SHARES=1e6) reduce but do not eliminate the risk. Seeding the market with 1e4 assets at creation is the recommended mitigation.",
          "severity": "high",
          "check": "Verify that new markets are seeded at creation or that the integration explicitly handles the share inflation edge case.",
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

• Filter by usage context — a finding that cannot apply given the stated
  integration purpose must be excluded, not included with a caveat.
• Source must be specific: firm + report name + section, not just a URL.
• check must be executable: a concrete thing an auditor can verify in code.
• If a protocol has no relevant public findings, record it with an empty
  known_issues list and note it in the summary.
• Do not read project source files — that is the audit step's job.
"""
