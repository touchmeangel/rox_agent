from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from ignite_core.debug_logger import DebugLogger, default_debug_logger
from ignite_core.mission import Mission
from ignite_core.reporting import Reporter, RichReporter
from ignite_core.repository_knowlage import RepositoryKnowledge
from ignite_core.rolling_window import RollingWindow
from ignite_core.steps.base import BaseStep, StepResult
from ignite_knowledge.descriptor import Descriptor
from ignite_knowledge.descriptors import load_descriptors


@dataclass
class _Candidate:
    contract: str
    descriptor: Descriptor
    matching_tags: list[str]
    static_hints: list[str]
    known_issue_hints: list[str]
    audit_delta_hint: str | None


def _candidates_for_context(context: dict) -> list[_Candidate]:
    tagging = context.get("tagging", {}).get("findings", {})
    contract_tags: dict[str, list[str]] = tagging.get("contract_tags", {})
    descriptors = load_descriptors()

    static_findings = context.get("static_analysis", {}).get("findings", {})
    known_issues = context.get("known_issues", {}).get("findings", {})
    audit_delta = context.get("audit_delta", {}).get("findings", {})

    candidates: list[_Candidate] = []

    for contract, tags in contract_tags.items():
        if not tags:
            continue
        tag_set = set(tags)

        for descriptor in descriptors:
            descriptor_tags = set(getattr(descriptor, "tags", []))
            matching = tag_set & descriptor_tags
            if not matching:
                continue

            static_hints = _static_hints_for(contract, descriptor, static_findings)
            ki_hints = _known_issue_hints_for(descriptor, known_issues)
            delta_hint = _audit_delta_hint_for(contract, audit_delta)

            candidates.append(
                _Candidate(
                    contract=contract,
                    descriptor=descriptor,
                    matching_tags=sorted(matching),
                    static_hints=static_hints,
                    known_issue_hints=ki_hints,
                    audit_delta_hint=delta_hint,
                )
            )

    return candidates


def _static_hints_for(
    contract: str,
    descriptor: Descriptor,
    static_findings: dict,
) -> list[str]:
    hints: list[str] = []
    descriptor_tags = set(getattr(descriptor, "tags", []))
    contract_name = contract.split("/")[-1].replace(".sol", "").lower()

    for bucket in ("confirmed", "suspected"):
        for finding in static_findings.get(bucket, []):
            loc = finding.get("location", {})
            finding_file = loc.get("file", "").lower()
            if (
                contract_name not in finding_file
                and contract.lower() not in finding_file
            ):
                continue

            check = finding.get("check", "")
            title = finding.get("title", "")
            if any(
                tag in check.lower() or tag in title.lower() for tag in descriptor_tags
            ):
                hint = f"[{bucket.upper()}] {check}: {title} @ {loc.get('file')}:{loc.get('lines', '?')}"
                hints.append(hint)

    return hints[:6]


def _known_issue_hints_for(
    descriptor: Descriptor,
    known_issues: dict,
) -> list[str]:
    hints: list[str] = []
    descriptor_tags = set(getattr(descriptor, "tags", []))

    for checklist in known_issues.get("protocol_checklists", []):
        for issue in checklist.get("known_issues", []):
            check = issue.get("check", "").lower()
            title = issue.get("title", "").lower()
            if any(tag in check or tag in title for tag in descriptor_tags):
                hints.append(
                    f"[{checklist['protocol']}] {issue['title']} "
                    f"({issue['severity']}) — {issue.get('description', '')[:120]}"
                )

    return hints[:4]


def _audit_delta_hint_for(contract: str, audit_delta: dict) -> str | None:
    if not audit_delta.get("diff_available"):
        return None

    changed = audit_delta.get("changed_files", [])
    contract_name = contract.split("/")[-1]
    if not any(contract_name in c for c in changed):
        return None

    risk = audit_delta.get("risk_level", "unknown")
    firm = (audit_delta.get("audit") or {}).get("firm", "unknown auditor")
    return f"Modified since {firm} audit (risk: {risk})"


def _build_knowledge(context: dict, candidate: _Candidate) -> RepositoryKnowledge:
    doc = context.get("documentation", {}).get("findings", {})
    static = context.get("static_analysis", {}).get("findings", {})
    fork = context.get("fork_analysis", {}).get("findings")
    audit_delta = context.get("audit_delta", {}).get("findings")

    previous_audits: list[dict[str, Any]] = []
    if audit_delta and audit_delta.get("audit"):
        previous_audits.append(audit_delta["audit"])

    diff: dict[str, Any] | None = None
    if audit_delta and audit_delta.get("diff_available"):
        diff = {
            "risk_level": audit_delta.get("risk_level"),
            "changed_files": audit_delta.get("changed_files", []),
            "diff_excerpt": audit_delta.get("diff_excerpt", ""),
        }

    risk_matrix = doc.get("contract_risk_matrix", [])
    related = [
        row["contract"]
        for row in risk_matrix
        if row.get("external_calls") and row.get("external_calls") != "None"
    ]

    return RepositoryKnowledge(
        documentation=doc,
        architecture={
            "notes": doc.get("architecture_notes", ""),
            "risk_matrix": risk_matrix,
        },
        standards=[
            t.get("standard", "") for t in doc.get("tokens", []) if t.get("standard")
        ],
        static_analysis=static,
        fork=fork,
        previous_audits=previous_audits,
        diff=diff,
        investigation_scope={
            "contracts": [candidate.contract]
            + [c for c in related if c != candidate.contract],
            "entrypoints": [],
            "ignore": [],
        },
    )


def _fmt_candidate(i: int, c: _Candidate) -> str:
    lines: list[str] = [
        f'<candidate index="{i}">',
        f"  <contract>{c.contract}</contract>",
        f'  <descriptor id="{c.descriptor.id}">{c.descriptor.name}</descriptor>',
        f"  <matched_tags>{', '.join(c.matching_tags)}</matched_tags>",
        "  <scenario>",
        "  " + c.descriptor.scenario.strip().replace("\n", "\n  "),
        "  </scenario>",
        "  <properties>",
    ]
    for prop in getattr(c.descriptor, "properties", []):
        lines.append(
            f'    <property id="{prop["id"]}">{prop.get("check", "").strip()[:300]}</property>'
        )
    lines.append("  </properties>")

    if c.static_hints:
        lines.append("  <static_analysis_hits>")
        lines.extend(f"    <hit>{h}</hit>" for h in c.static_hints)
        lines.append("  </static_analysis_hits>")

    if c.known_issue_hints:
        lines.append("  <known_issue_hits>")
        lines.extend(f"    <hit>{h}</hit>" for h in c.known_issue_hints)
        lines.append("  </known_issue_hits>")

    if c.audit_delta_hint:
        lines.append(f"  <audit_delta>{c.audit_delta_hint}</audit_delta>")

    lines.append("</candidate>")
    return "\n".join(lines)


def _fmt_context_summary(context: dict) -> str:
    sections: list[str] = []

    doc = context.get("documentation", {})
    if doc.get("success"):
        findings = doc.get("findings", {})
        sections.append(
            "<documentation>\n"
            f"  <summary>{findings.get('protocol_summary', '(none)')}</summary>\n"
            f"  <solc_version>{findings.get('solc_version', '?')}</solc_version>\n"
            f"  <contract_count>{len(findings.get('contract_risk_matrix', []))}</contract_count>\n"
            "</documentation>"
        )

    fork = context.get("fork_analysis", {})
    if fork.get("success"):
        f = fork.get("findings", {})
        if f.get("is_fork"):
            up = f.get("upstream") or {}
            sections.append(
                "<fork_analysis>\n"
                f"  <upstream_protocol>{up.get('protocol', '?')}</upstream_protocol>\n"
                f"  <base_commit>{up.get('base_commit', '?')}</base_commit>\n"
                f"  <modified_file_count>{len(f.get('modified_files', []))}</modified_file_count>\n"
                "</fork_analysis>"
            )

    audit_delta = context.get("audit_delta", {})
    if audit_delta.get("success"):
        a = audit_delta.get("findings", {})
        if a.get("audit_found"):
            sections.append(
                "<audit_delta>\n"
                f"  <firm>{a['audit']['firm']}</firm>\n"
                f"  <risk_level>{a['risk_level']}</risk_level>\n"
                f"  <lines_changed_since_audit>+{a['insertions']}/-{a['deletions']}</lines_changed_since_audit>\n"
                "</audit_delta>"
            )

    ki = context.get("known_issues", {})
    if ki.get("success"):
        checklists = ki.get("findings", {}).get("protocol_checklists", [])
        total = sum(len(c["known_issues"]) for c in checklists)
        sections.append(
            "<known_issues>\n"
            f"  <relevant_issue_count>{total}</relevant_issue_count>\n"
            f"  <protocol_count>{len(checklists)}</protocol_count>\n"
            "</known_issues>"
        )

    return "\n\n".join(sections) if sections else "<no_prior_context_available/>"


_CONTEXT_KEYS = (
    "documentation",
    "static_analysis",
    "tagging",
    "fork_analysis",
    "audit_delta",
    "known_issues",
)

_SUBMIT_SPEC: dict = {
    "name": "submit_missions",
    "description": (
        "Submit the list of missions to create. Call this once, after you have "
        "decided which candidates warrant investigation."
    ),
    "input_schema": {
        "type": "object",
        "required": ["missions", "skipped"],
        "properties": {
            "missions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "candidate_index",
                        "property_ids",
                        "hints",
                        "rationale",
                    ],
                    "properties": {
                        "candidate_index": {
                            "type": "integer",
                            "description": "The 0-based index into the CANDIDATES list above.",
                        },
                        "property_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Which property ids within the descriptor to focus on. Use [] for all.",
                        },
                        "hints": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Concrete, actionable hints for the worker: specific line numbers, "
                                "function names, or patterns to check first. Distill what you know "
                                "from prior steps that the worker doesn't have time to rediscover."
                            ),
                        },
                        "rationale": {
                            "type": "string",
                            "description": "One sentence: why this candidate warrants a mission.",
                        },
                    },
                },
            },
            "skipped": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["candidate_index", "reason"],
                    "properties": {
                        "candidate_index": {"type": "integer"},
                        "reason": {
                            "type": "string",
                            "description": "Concrete reason this candidate is not worth investigating.",
                        },
                    },
                },
            },
        },
    },
}


class CoordinatorStep(BaseStep):
    name = "coordinator"

    def select_context(self, global_context: dict) -> dict:
        return {k: v for k, v in global_context.items() if k in _CONTEXT_KEYS}

    def termination_criteria_prompt(self) -> str:
        return """<termination_criteria>
You have exactly ONE tool available: `submit_missions`. This is not an iterative
loop — call it once, with your complete set of decisions for every candidate listed
in <candidates> above, and you are done. There is no submit_findings, no
report_blocker, no report_failed, and no follow-up turn to add anything afterward.
</termination_criteria>"""

    def system_prompt(self) -> str:
        return f"""<role>
You are the triage coordinator for an automated Solidity/EVM security audit pipeline.
You make ONE classification decision per candidate, in a single tool call — you do not
operate in a multi-turn loop, and you have no tools besides submit_missions.
</role>

{self.phase_scope_note()}

<environment>
{self.environment_prompt()}
</environment>

{self.termination_criteria_prompt()}"""

    def environment_prompt(self) -> str:
        return (
            f"- Repository: {self.github_repo_path}\n"
            f"- Workspace:  {self.project_path}\n"
            "- Role: Triage coordinator. You have full prior-step context. "
            "No file access — all evidence is already in this prompt."
        )

    def run(self, context: dict) -> StepResult:
        default_debug_logger.log_event("step_start", step=self.name, max_iterations=1)

        candidates = _candidates_for_context(context)

        if not candidates:
            return StepResult(
                step_name=self.name,
                success=True,
                findings={"missions": [], "skipped": []},
                summary="No tag-matched candidates found — no missions created.",
                iterations_used=0,
                missions=[],
            )

        with RollingWindow(max_lines=8) as window:
            prompt = self.initial_prompt_with_candidates(context, candidates)
            reporter = RichReporter(window)
            turn = self.llm.chat_with_tools(
                messages=[{"role": "user", "content": prompt}],
                tools=[_SUBMIT_SPEC],
                system=self.system_prompt(),
                reporter=reporter,
                step_name=self.name,
                show_thinking=True,
            )

        default_debug_logger.log_event(
            "iteration_start", step=self.name, iteration=1, iterations_left=0
        )

        raw: dict = {"missions": [], "skipped": []}
        if turn.tool_calls:
            call = turn.tool_calls[0]
            if call.name == "submit_missions":
                raw = call.input
        elif turn.text:
            try:
                raw = json.loads(turn.text)
            except json.JSONDecodeError:
                pass

        missions = _build_missions(raw.get("missions", []), candidates, context)

        default_debug_logger.log_event(
            "step_complete",
            step=self.name,
            iteration=1,
            missions_created=len(missions),
            skipped=len(raw.get("skipped", [])),
        )

        return StepResult(
            step_name=self.name,
            success=True,
            findings=raw,
            summary=(
                f"Created {len(missions)} mission(s) from {len(candidates)} candidates "
                f"({len(raw.get('skipped', []))} skipped)."
            ),
            iterations_used=1,
            missions=missions,
        )

    def initial_prompt(
        self, context: dict, reporter: Reporter, debug_logger: DebugLogger
    ) -> str:
        return self.initial_prompt_with_candidates(context, [])

    def initial_prompt_with_candidates(
        self, context: dict, candidates: list[_Candidate]
    ) -> str:
        context_summary = _fmt_context_summary(context)
        candidates_section = "\n\n".join(
            _fmt_candidate(i, c) for i, c in enumerate(candidates)
        )

        return f"""<scope>
You are the triage coordinator for this audit run. You decide which (contract,
vulnerability class) candidates below warrant a dedicated worker investigation. You do
NOT investigate, confirm, or disprove anything yourself — that is the worker phase's job,
with its own iteration budget and tool access. Your only output is a routing decision
plus hints that save the worker time.
</scope>

<prior_step_context>
{context_summary}
</prior_step_context>

<candidates total="{len(candidates)}" note="pre-filtered by tag intersection between contract tags and descriptor tags">
{candidates_section}
</candidates>

<task>
For every candidate above, decide YES (create a mission) or NO (skip). These candidates
have already been narrowed by tag matching — your job is not to re-filter by relevance,
it's to triage whether there's enough signal to justify a worker looking at it.

Bias toward YES. A missed real vulnerability here is unrecoverable — nothing downstream
will ever look at this (contract, descriptor) pair again. The cost of a false positive is
one wasted worker iteration; the cost of a false negative is a real bug in production.

Create a mission when ANY of the following hold:
  - A static analysis tool flagged this contract for this class of issue
  - A known issue in a dependency matches this descriptor
  - The contract was modified after an audit (audit delta hit)
  - The descriptor's scenario plausibly applies to what this contract does
  - You are uncertain — uncertainty resolves to YES

Skip only with a concrete, stated reason:
  - The contract is a pure interface or library with no state
  - The descriptor requires a pattern (e.g. block.timestamp) that provably does not exist
    in this contract, based on evidence already given to you above
  - The contract inherits OpenZeppelin/Solmate unmodified and the descriptor targets a
    pattern OZ/Solmate already prevents by construction
</task>

<output_format>
Call submit_missions exactly once with your complete decision set. For every candidate
you create a mission for, include property_ids (which properties within the descriptor
to focus on — [] means all), hints, and a one-sentence rationale. For every candidate you
skip, include a concrete reason.
</output_format>

<rules>
- Hints must be specific and actionable: "flipResult on line 42 uses block.timestamp as
  the only entropy source" beats "check randomness usage." The worker has limited
  iterations and reads your hints first; vague hints waste that budget.
- Every candidate index above must appear in either missions or skipped — none may be
  silently omitted.
- Do not describe how you would exploit anything, write example attack code, or sketch a
  PoC — see phase_scope above. That is explicitly out of scope for this phase.
- This is a single call with no follow-up turn — include everything now.
</rules>"""


def _build_missions(
    raw_missions: list[dict],
    candidates: list[_Candidate],
    context: dict,
) -> list[Mission]:
    missions: list[Mission] = []

    for raw in raw_missions:
        idx = raw.get("candidate_index")
        if idx is None or idx >= len(candidates):
            default_debug_logger.log_event(
                "coordinator_invalid_candidate_index",
                step=CoordinatorStep.name,
                candidate_index=idx,
                candidate_count=len(candidates),
            )
            continue

        candidate = candidates[idx]
        knowledge = _build_knowledge(context, candidate)

        mission = Mission(
            id=f"M-{uuid.uuid4().hex[:32].upper()}",
            contract=candidate.contract,
            vulnerability=candidate.descriptor.name,
            descriptor=candidate.descriptor,
            knowledge=knowledge,
            hints=[
                *candidate.static_hints,
                *candidate.known_issue_hints,
                *(
                    []
                    if not candidate.audit_delta_hint
                    else [candidate.audit_delta_hint]
                ),
                *raw.get("hints", []),
            ],
        )
        missions.append(mission)

    return missions
