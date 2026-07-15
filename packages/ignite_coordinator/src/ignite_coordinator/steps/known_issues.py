from __future__ import annotations

from dataclasses import dataclass, field

from ignite_core.debug_logger import DebugLogger
from ignite_core.graph import Graph
from ignite_core.reporting import Reporter
from ignite_core.search_state import SearchState
from ignite_core.steps.base import BaseStep, StepResult
from ignite_core.steps.config import StepConfig
from ignite_core.tools import dispatch
from ignite_knowledge.descriptor import Descriptor
from ignite_knowledge.descriptors import load_descriptors
from ignite_knowledge.protocol_tags import match_protocol_tags


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
    tagging_findings = context.get("semantic_tagging", {}).get("findings", {})

    targets: list[ProtocolTarget] = []
    seen_names: set[str] = set()

    if fork_findings.get("is_fork") and fork_findings.get("upstream"):
        upstream = fork_findings["upstream"]
        name = upstream.get("protocol", "").strip()
        if name and name not in seen_names:
            seen_names.add(name)
            targets.append(
                ProtocolTarget(
                    name=name,
                    purpose=f"Fork upstream (base commit {upstream.get('base_commit', '?')})",
                    version="",
                    is_fork_upstream=True,
                )
            )

    detected_protocols_map = tagging_findings.get("detected_protocols", {})
    for protocols in detected_protocols_map.values():
        if isinstance(protocols, list):
            for p in protocols:
                name = p.strip()
                if name and name not in seen_names:
                    seen_names.add(name)
                    targets.append(
                        ProtocolTarget(
                            name=name,
                            purpose="Identified as contract external integration",
                            version="",
                            is_fork_upstream=False,
                        )
                    )

    for p in doc_findings.get("external_protocols", []):
        name = p.get("name", "").strip()
        if name and name not in seen_names:
            seen_names.add(name)
            targets.append(
                ProtocolTarget(
                    name=name,
                    purpose=p.get("purpose", ""),
                    version=p.get("version", ""),
                    is_fork_upstream=False,
                )
            )

    return targets


def _curated_coverage_for_target(target: ProtocolTarget) -> list[Descriptor]:
    matched_tag_ids = set(match_protocol_tags(target.name))
    if not matched_tag_ids:
        return []
    return [
        d for d in load_descriptors() if matched_tag_ids & set(getattr(d, "tags", []))
    ]


def _prefetch_protocol(
    target: ProtocolTarget,
    project_path: str,
    repo_graph: Graph,
    reporter: Reporter,
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
            repo_graph,
            reporter=reporter,
            search_state=search_state,
            debug_logger=debug_logger,
        )
        out = (result.stdout or "").strip()
        if out:
            target.prefetch_results.append(
                f"Query: {query}\n{out[:max_chars_per_result]}"
            )


def _fmt_target(target: ProtocolTarget, curated: list[Descriptor]) -> str:
    tag = " [FORK UPSTREAM]" if target.is_fork_upstream else ""
    lines = [
        f"Protocol: {target.name}{tag}",
        f"Version:  {target.version or 'unknown'}",
        f"Usage:    {target.purpose}",
    ]
    if curated:
        lines.append(
            "\nAlready curated (a worker mission already exists for each of "
            "these specific issues — do not spend a search re-discovering "
            "them, but keep researching this protocol for anything else):"
        )
        for d in curated:
            first_line = d.scenario.strip().splitlines()[0][:160]
            lines.append(f"  - {d.name}: {first_line}")
    if target.prefetch_results:
        lines.append("\nPre-fetched search results:")
        for r in target.prefetch_results:
            lines.append(f"\n{r}")
    else:
        lines.append("\nPre-fetch: (no results returned)")
    return "\n".join(lines)


class KnownIssuesStep(BaseStep):
    name = "known_issues"

    def __init__(
        self, cfg: StepConfig, per_target: float = 0.0, min_iterations: int = 10
    ):
        self.per_target = per_target
        self.min_iterations = min_iterations
        super().__init__(cfg)

    def iteration_budget(self, context: dict) -> int:
        targets = _build_targets(context)

        return min(
            self.cfg.max_iterations,
            self.min_iterations + int(len(targets) * self.per_target)
        )

    def select_context(self, global_context: dict) -> dict:
        return {
            "documentation": global_context.get("documentation", {}),
            "fork_analysis": global_context.get("fork_analysis", {}),
            "semantic_tagging": global_context.get("semantic_tagging", {}),
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
            return "findings must be json object (python dict)"

        if not findings.get("summary"):
            return "summary is not set which is required"

        checklists = findings.get("protocol_checklists")
        if not isinstance(checklists, list):
            return "invalid protocol_checklists, must be a defined list"

        return None

    def initial_prompt(
        self, context: dict, reporter: Reporter, debug_logger: DebugLogger
    ) -> str:
        doc_findings = context.get("documentation", {}).get("findings", {})
        protocol_summary = doc_findings.get("protocol_summary", "(none)")

        targets = _build_targets(context)
        curated_by_target = {t.name: _curated_coverage_for_target(t) for t in targets}

        search_state = SearchState(max_calls=len(targets) * 2)
        for t in targets:
            _prefetch_protocol(
                t,
                self.project_path,
                self.repo_graph,
                reporter,
                debug_logger,
                search_state,
            )

        targets_section = "\n\n────────────────────────────────────\n\n".join(
            _fmt_target(t, curated_by_target[t.name]) for t in targets
        )
        prefetch_note = (
            f"Pre-fetched {sum(len(t.prefetch_results) for t in targets)} "
            f"search results across {len(targets)} protocol(s). "
            "Use web_fetch on any URLs that look promising."
        )

        return f"""<audit_step name="known_issues">

<scope>
Research publicly disclosed audit findings for the external protocols and fork-upstream
this codebase depends on. This step does NOT read the project source and does NOT judge
whether a known issue actually applies to how this specific code is written — it only
compiles a checklist of relevant, previously disclosed issues for a later step to verify
against the code. Do not write or reference exploit code in this step.
</scope>

<project_path>{self.project_path}</project_path>

<protocol_context>
{protocol_summary}
</protocol_context>

<research_targets note="{prefetch_note}">
{targets_section}
</research_targets>

<task>
For each protocol listed above, find every known audit finding or disclosed vulnerability
that is relevant to HOW that protocol is used here. The "Usage" field tells you the
integration context — use it to decide which findings actually apply.

<step id="0" title="Respect already-curated coverage, but don't stop there">
Any protocol with an "Already curated" list already has a dedicated worker mission queued
for each of those specific issues elsewhere in the pipeline — do not spend a search
re-discovering them, and do not list them again in your own findings. This does NOT mean
skip researching that protocol: keep looking for anything else publicly known about it. One
curated issue existing says nothing about whether it's the only one — prior coverage is a
reason not to duplicate what's already found, not a reason to stop looking.
</step>

<step id="1" title="Extract from pre-fetched results">
For any URL in the results above that looks like an audit report or finding list, call
web_fetch to read the full content. Prioritise: Cantina, Trail of Bits, OpenZeppelin,
Spearbit, Code4rena, Sherlock, Immunefi, and the protocol's own GitHub /audits folder.
</step>

<step id="2" title="Fill gaps with additional searches">
If pre-fetched results are thin for a protocol, run at most 1 additional targeted search per
protocol: web_search "protocol version audit cantina OR spearbit OR trail-of-bits".
Once you have findings from a credible source, stop searching that protocol. If nothing
surfaces after the additional search, record empty known_issues and move on — do not keep
rephrasing the same query.
</step>

<step id="3" title="Filter by usage connection">
For each finding ask: does the Usage description create the conditions for this
vulnerability?
  - "Used for swapping tokens" → share inflation from market creation does NOT apply
  - "Creates new lending markets" → share inflation DOES apply
Exclude findings that cannot apply — do not include them with a caveat.
</step>

<step id="4" title="Write actionable checks">
Each finding needs a concrete check — not "review X" but "verify that function Y enforces
invariant Z before calling into protocol". This check is a task for the later step that
reads the source, not something to verify yourself here.
</step>
</task>

<output_format>
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
  "summary": "Researched protocols. Found relevant known issues."
}}
</output_format>

<rules>
<rule>Maximum 1 additional search per protocol beyond the pre-fetch.</rule>
<rule>Source must be specific: firm + report name + section, not just a URL.</rule>
<rule>check must be a concrete thing for a later step to verify in code — not something you verify yourself here.</rule>
<rule>If a protocol has no relevant public findings beyond what's already curated, record empty known_issues.</rule>
<rule>Do not read project source files — that is a later step's job.</rule>
<rule>Do not write or reference exploit code in this step.</rule>
</rules>

</audit_step>"""
