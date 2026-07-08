from __future__ import annotations

import json
from dataclasses import dataclass

from ignite_core.debug_logger import DebugLogger, default_debug_logger
from ignite_core.reporting import Reporter, RichReporter
from ignite_core.rolling_window import RollingWindow
from ignite_core.steps.base import BaseStep, StepResult
from ignite_knowledge.descriptor import Descriptor
from ignite_knowledge.descriptors import load_descriptors


@dataclass
class _Candidate:
    contract: str
    descriptor: Descriptor
    matching_tags: list[str]
    tag_evidence: dict[str, list[str]]
    detected_protocols: list[str]
    static_hints: list[str]
    known_issue_hints: list[str]
    audit_delta_hint: str | None


def _candidates_for_context(context: dict) -> list[_Candidate]:
    semantic = context.get("semantic_tagging", {}).get("findings", {})

    semantic_tags: dict[str, list[str]] = semantic.get("semantic_tags", {})
    tag_evidence: dict[str, dict[str, list[str]]] = semantic.get("tag_evidence", {})
    detected_protocols_by_contract: dict[str, list[str]] = semantic.get(
        "detected_protocols", {}
    )

    descriptors = load_descriptors()

    static_findings = context.get("static_analysis", {}).get("findings", {})
    known_issues = context.get("known_issues", {}).get("findings", {})
    audit_delta = context.get("audit_delta", {}).get("findings", {})

    candidates: list[_Candidate] = []

    for contract, tags in semantic_tags.items():
        if not tags:
            continue

        tag_set = set(tags)
        contract_evidence = tag_evidence.get(contract, {})

        for descriptor in descriptors:
            descriptor_tags = set(getattr(descriptor, "tags", []))
            matching = tag_set & descriptor_tags

            if not matching:
                continue

            static_hints = _static_hints_for(
                contract,
                descriptor,
                static_findings,
            )

            ki_hints = _known_issue_hints_for(
                descriptor,
                known_issues,
            )

            delta_hint = _audit_delta_hint_for(
                contract,
                audit_delta,
            )

            candidates.append(
                _Candidate(
                    contract=contract,
                    descriptor=descriptor,
                    matching_tags=sorted(matching),
                    tag_evidence={
                        tag: contract_evidence.get(tag, []) for tag in sorted(matching)
                    },
                    detected_protocols=detected_protocols_by_contract.get(contract, []),
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


def _fmt_candidate(i: int, c: _Candidate) -> str:
    lines: list[str] = [
        f'<candidate index="{i}">',
        f"  <contract>{c.contract}</contract>",
        f'  <descriptor id="{c.descriptor.id}">{c.descriptor.name}</descriptor>',
        "  <matched_semantic_tags>",
    ]

    for tag in c.matching_tags:
        lines.append(f'    <tag id="{tag}">')

        observations = c.tag_evidence.get(tag, [])
        if observations:
            lines.append("      <evidence>")
            lines.extend(
                f"        <observation>{obs}</observation>" for obs in observations
            )
            lines.append("      </evidence>")

        lines.append("    </tag>")

    lines.append("  </matched_semantic_tags>")

    if c.detected_protocols:
        lines.append("  <protocols>")
        lines.extend(f"    <protocol>{p}</protocol>" for p in c.detected_protocols)
        lines.append("  </protocols>")

    lines.extend(
        [
            "  <scenario>",
            "  " + c.descriptor.scenario.strip().replace("\n", "\n  "),
            "  </scenario>",
            "  <properties>",
        ]
    )

    for prop in getattr(c.descriptor, "properties", []):
        lines.append(
            f'    <property id="{prop["id"]}">'
            f"{prop.get('check', '').strip()[:300]}"
            "</property>"
        )

    lines.append("  </properties>")

    if c.static_hints:
        lines.append("  <static_analysis_hits>")
        lines.extend(f"    <hit>{hint}</hit>" for hint in c.static_hints)
        lines.append("  </static_analysis_hits>")

    if c.known_issue_hints:
        lines.append("  <known_issue_hits>")
        lines.extend(f"    <hit>{hint}</hit>" for hint in c.known_issue_hints)
        lines.append("  </known_issue_hits>")

    if c.audit_delta_hint:
        lines.append(f"  <audit_delta>{c.audit_delta_hint}</audit_delta>")

    lines.append("</candidate>")

    return "\n".join(lines)


_CONTEXT_KEYS = (
    "documentation",
    "static_analysis",
    "semantic_tagging",
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


class PlanningStep(BaseStep):
    name = "planning"

    def select_context(self, global_context: dict) -> dict:
        return {k: v for k, v in global_context.items() if k in _CONTEXT_KEYS}

    def termination_criteria_prompt(self) -> str:
        return """<termination_criteria>
You have exactly ONE tool available: `submit_missions`. This is not an iterative
loop — call it once, with your complete set of decisions for every candidate listed
in <candidates> above, and you are done. There is no submit_findings, no
report_blocker, no report_failed, and no follow-up turn to add anything afterward.
</termination_criteria>"""

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
        missions_created = len(raw.get("missions", []))

        default_debug_logger.log_event(
            "step_complete",
            step=self.name,
            iteration=1,
            missions_created=missions_created,
            skipped=len(raw.get("skipped", [])),
        )

        return StepResult(
            step_name=self.name,
            success=True,
            findings=raw,
            summary=(
                f"Created {missions_created} mission(s) from {len(candidates)} candidates "
                f"({len(raw.get('skipped', []))} skipped)."
            ),
            iterations_used=1,
        )

    def initial_prompt(
        self, context: dict, reporter: Reporter, debug_logger: DebugLogger
    ) -> str:
        return self.initial_prompt_with_candidates(context, [])

    def initial_prompt_with_candidates(
        self, context: dict, candidates: list[_Candidate]
    ) -> str:
        candidates_section = "\n\n".join(
            _fmt_candidate(i, c) for i, c in enumerate(candidates)
        )

        return f"""<scope>

You are the mission planner for this audit.

Your responsibility is to decide which pre-generated investigation candidates
should receive a dedicated worker mission.

You do NOT verify whether a descriptor matches a contract.
You do NOT investigate vulnerabilities.
You do NOT confirm or reject findings.

Your only responsibility is allocating worker time efficiently.

</scope>

<candidates
    total="{len(candidates)}"
    note="Generated automatically by matching descriptor semantic requirements against the repository semantic index. SemanticTagging has already verified why the required semantic tags apply."
>

{candidates_section}

</candidates>

<task>

Each candidate already passed semantic matching.

The SemanticTagging step determined that the descriptor's required architectural
properties are present in the contract and collected factual implementation
evidence explaining those matches.

Do NOT spend time deciding whether a descriptor applies to a contract.
Assume the semantic classification is correct.

Your job is only to decide whether allocating worker time to investigate this
candidate is worthwhile.

Bias toward creating missions.

A missed investigation is permanent.
A false positive only costs one worker.

Create a mission whenever investigation is likely to produce useful security
coverage.

Strong reasons include:

- Relevant static-analysis findings exist.
- A dependency has known issues related to this descriptor.
- The contract changed after a previous audit.
- The contract is security-critical or economically important.
- Strong semantic evidence indicates meaningful implementation complexity.
- You are uncertain.

When uncertain, create the mission.

Skip a candidate only when worker time would almost certainly be wasted.

Examples include:

- Pure interface.
- Pure library with no meaningful implementation.
- Abstract contract containing no executable logic.
- The supplied evidence already proves the descriptor cannot be investigated
  meaningfully.

</task>

<output_format>

Call submit_missions exactly once.

Every candidate must appear exactly once.

For every created mission provide:

- candidate_index
- property_ids ([] means investigate the full descriptor)
- hints
- rationale

For every skipped candidate provide:

- candidate_index
- reason

</output_format>

<hints>

Workers read your hints before reading the contract.

Use hints to save investigation time.

Prefer pointing workers toward:

- semantic evidence collected during classification
- relevant static-analysis findings
- protocol-specific known issues
- recently modified code
- concrete functions or implementation details

Avoid repeating the descriptor itself.

</hints>

<rules>

- Every candidate index must appear in either missions or skipped.
- Treat SemanticTagging output as trusted input; do not re-classify contracts.
- Keep rationales concise.
- Hints should be concrete and actionable.
- Do not investigate vulnerabilities yourself.
- Do not describe attacks, exploits, or PoCs.
- This is a single tool call with no follow-up.

</rules>"""
