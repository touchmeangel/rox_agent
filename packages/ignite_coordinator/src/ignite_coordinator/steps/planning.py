from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ignite_core.debug_logger import DebugLogger, default_debug_logger
from ignite_core.mission import Mission, MissionLeg
from ignite_core.reporting import Reporter
from ignite_core.repository_knowledge import InvestigationScope, RepositoryKnowledge
from ignite_core.steps.base import BaseStep
from ignite_core.steps.config import StepConfig
from ignite_core.tools import GRAPH_QUERY_SPEC, READ_FILE_SPEC, REVEAL_TOOL_SPEC
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
    cross_reference_hints: list[str] = field(default_factory=list)
    confirmed_related: list[str] = field(default_factory=list)


def _confirmed_chain_pairs(
    semantic_tags: dict[str, list[str]], documentation: dict
) -> dict[str, list[str]]:
    risk_matrix = documentation.get("contract_risk_matrix", [])
    calls_by_contract = {
        row.get("contract", ""): (row.get("external_calls") or "").lower()
        for row in risk_matrix
    }
    consumers = [
        c for c, t in semantic_tags.items() if "external_rate_price_consumer" in t
    ]
    sources = [c for c, t in semantic_tags.items() if "pooled_share_vault" in t]

    pairs: dict[str, list[str]] = {}
    for consumer in consumers:
        calls_text = calls_by_contract.get(consumer, "")
        matched = [
            s for s in sources if s != consumer and Path(s).stem.lower() in calls_text
        ]
        if matched:
            pairs[consumer] = matched
    return pairs


def _index_of(
    candidates: list[_Candidate], contract: str, descriptor_id: str
) -> int | None:
    return next(
        (
            i
            for i, c in enumerate(candidates)
            if c.contract == contract and c.descriptor.id == descriptor_id
        ),
        None,
    )


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

            static_hints = _static_hints_for(contract, descriptor, static_findings)
            ki_hints = _known_issue_hints_for(descriptor, known_issues)
            delta_hint = _audit_delta_hint_for(contract, audit_delta)

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
    contract: str, descriptor: Descriptor, static_findings: dict
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


def _known_issue_hints_for(descriptor: Descriptor, known_issues: dict) -> list[str]:
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

    exploitation_context = getattr(c.descriptor, "exploitation_context", "") or ""
    if exploitation_context.strip():
        lines.extend(
            [
                "  <exploitation_context>",
                "  " + exploitation_context.strip()[:800].replace("\n", "\n  "),
                "  </exploitation_context>",
            ]
        )

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


def _fmt_target_file(contract: str, entries: list[tuple[int, _Candidate]]) -> str:
    lines: list[str] = [
        f'<target_file path="{contract}" candidate_count="{len(entries)}">',
    ]
    if len(entries) > 1:
        lines.append(
            "  <peek_once_note>"
            f"All {len(entries)} candidates below share this exact file. "
            "If you peek it, peek it ONCE — apply that single observation to "
            "every candidate listed under this file. Do not call read_file on "
            "this path more than once."
            "</peek_once_note>"
        )
    lines.extend(_fmt_candidate(i, c) for i, c in entries)
    lines.append("</target_file>")
    return "\n".join(lines)


def _group_candidates_by_contract(
    candidates: list[_Candidate],
) -> list[tuple[str, list[tuple[int, _Candidate]]]]:
    groups: dict[str, list[tuple[int, _Candidate]]] = {}
    for i, c in enumerate(candidates):
        groups.setdefault(c.contract, []).append((i, c))
    return list(groups.items())


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
        "Submit the final list of prioritized worker missions. Call this once "
        "after resolving target ambiguities using the peeking tools."
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
                        "candidate_indices",
                        "property_ids",
                        "hints",
                        "rationale",
                    ],
                    "properties": {
                        "candidate_indices": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "minItems": 1,
                            "description": (
                                "0-based indices into the CANDIDATES list. A single-element "
                                "list is the normal case — one candidate, one worker. Include "
                                "more than one ONLY when the candidates have a genuine "
                                "directional dependency (typically: one candidate's `scenario` "
                                "or `exploitation_context` explicitly references another "
                                "descriptor by name — e.g. 'see X for whether the source side "
                                "ALSO has...', 'assume Y works correctly and focus only on...'). "
                                "Bundling means ONE worker verifies the full chain end-to-end "
                                "and returns ONE combined verdict, not two independent ones. "
                                "Do not bundle candidates just because they share a file or a "
                                "vague theme — the dependency should be explicit in the "
                                "descriptor text, not inferred from proximity."
                            ),
                        },
                        "property_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Specific property IDs to target, from any bundled descriptor's "
                                "properties. Use [] for all."
                            ),
                        },
                        "hints": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Concrete, actionable hints for the worker: specific lines, functions, "
                                "or storage states. Incorporate your findings from peeking if relevant."
                            ),
                        },
                        "rationale": {
                            "type": "string",
                            "description": (
                                "One sentence. For bundled candidates, name the dependency "
                                "explicitly: which is upstream/source, which is downstream/"
                                "consumer, and why one combined mission is more efficient than "
                                "two isolated ones."
                            ),
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
                            "description": "Clear structural justification for skipping this candidate.",
                        },
                    },
                },
            },
        },
    },
}


class PlanningStep(BaseStep):
    name = "planning"

    def __init__(
        self, cfg: StepConfig, per_candidate: float = 2.0, base_iterations: int = 10
    ):
        self.per_candidate = per_candidate
        self.base_iterations = base_iterations
        super().__init__(cfg)

    def iteration_budget(self, context: dict) -> int:
        candidates = _candidates_for_context(context)

        return min(
            self.cfg.max_iterations,
            self.base_iterations + int(len(candidates) * self.per_candidate),
        )

    @property
    def submission_tool_name(self) -> str:
        return "submit_missions"

    def submission_tool_spec(self) -> dict:
        return _SUBMIT_SPEC

    def step_tools(self) -> list[dict]:
        return [REVEAL_TOOL_SPEC, READ_FILE_SPEC, GRAPH_QUERY_SPEC]

    def terminal_tools(self) -> list[dict]:
        return [
            self.submission_tool_spec(),
            *self._non_submission_terminal_tool_specs(),
        ]

    def select_context(self, global_context: dict) -> dict:
        self._context = {k: v for k, v in global_context.items() if k in _CONTEXT_KEYS}
        return self._context

    def initial_prompt(
        self, context: dict, reporter: Reporter, debug_logger: DebugLogger
    ) -> str:
        self._context = context
        self._candidates = _candidates_for_context(context)
        return self.initial_prompt_with_candidates(context, self._candidates)

    def validate_complete(self, findings: dict) -> str | None:
        if not isinstance(findings, dict):
            return "Findings must be a JSON object (python dict)."

        missions = findings.get("missions", [])
        skipped = findings.get("skipped", [])

        if not isinstance(missions, list) or not isinstance(skipped, list):
            return "'missions' and 'skipped' must be lists."

        candidates = getattr(self, "_candidates", [])
        n_candidates = len(candidates)
        covered_indices: set[int] = set()

        for i, m in enumerate(missions):
            if not isinstance(m, dict):
                return f"Mission at index {i} must be a JSON object."
            idxs = m.get("candidate_indices")
            if not isinstance(idxs, list) or not idxs:
                return f"Mission at index {i} must have a non-empty 'candidate_indices' list."
            for idx in idxs:
                if not isinstance(idx, int) or idx < 0 or idx >= n_candidates:
                    return f"Mission at index {i} has an out-of-bounds or invalid candidate index: {idx!r}."
                if idx in covered_indices:
                    return f"Candidate index {idx} is duplicated across selections."
                covered_indices.add(idx)

        for i, s in enumerate(skipped):
            if not isinstance(s, dict):
                return f"Skipped entry at index {i} must be a JSON object."
            idx = s.get("candidate_index")
            if not isinstance(idx, int) or idx < 0 or idx >= n_candidates:
                return f"Skipped entry at index {i} has an out-of-bounds or invalid 'candidate_index'."
            if idx in covered_indices:
                return f"Candidate index {idx} is duplicated across selections."
            covered_indices.add(idx)

        missing = set(range(n_candidates)) - covered_indices
        if missing:
            return f"Missing candidate indices: {sorted(missing)}. Every single candidate index must appear."

        return None

    def post_process_findings(self, findings: dict) -> dict:
        candidates = getattr(self, "_candidates", [])
        context = getattr(self, "_context", {})
        missions = _build_missions(
            findings.get("missions", []), candidates, context, self.name
        )
        return {
            "missions": missions,
            "skipped": findings.get("skipped", []),
        }

    def get_success_summary(self, tool_input: dict, findings: dict) -> str:
        candidates = getattr(self, "_candidates", [])
        missions = findings.get("missions", [])
        skipped = findings.get("skipped", [])
        return (
            f"Successfully organized {len(missions)} worker mission(s) "
            f"and skipped {len(skipped)} out of {len(candidates)} total candidates."
        )

    def environment_prompt(self) -> str:
        return (
            f"- Workspace Project Path: {self.project_path}\n"
            "- Role: Triage Coordinator. You have full context summaries from prior stages.\n"
            "- Capabilities: You can read targeted source file excerpts via tools to resolve structural ambiguities."
        )

    def termination_criteria_prompt(self) -> str:
        return """<termination_criteria>
You have two operational phases:
1. (Optional) Resolve uncertainties by issuing individual `read_file` or `graph_query`
   calls to check for empty scaffolds, mocks, pure interfaces, or dead code.
2. Complete your allocation by executing `submit_missions` exactly once. Your submission
   must explicitly account for every candidate.
</termination_criteria>"""

    def initial_prompt_with_candidates(
        self, context: dict, candidates: list[_Candidate]
    ) -> str:
        grouped = _group_candidates_by_contract(candidates)
        unique_files = len(grouped)

        candidates_section = "\n\n".join(
            _fmt_target_file(contract, entries) for contract, entries in grouped
        )

        return f"""<scope>
You are the mission planner for this audit.
Your responsibility is to decide which pre-generated investigation candidates should receive a dedicated worker mission.

You do NOT verify whether a descriptor matches a contract.
You do NOT investigate vulnerabilities.
You do NOT confirm or reject findings.

Your only responsibility is allocating worker time efficiently.
</scope>

<candidates total="{len(candidates)}" unique_files="{unique_files}">
{candidates_section}
</candidates>

<investigation_capabilities>
If a candidate's provided evidence leaves you genuinely uncertain, two tools can resolve it:

- `read_file` — read a source file's actual implementation when you need to check whether
  it's an empty scaffold, a mock, or a pure interface with zero executable logic.
- `graph_query` — check structural facts cheaply without reading source: `inherits`/
  `inherited_by` to confirm a contract is actually part of the inheritance chain, or
  `dead_code` to check whether a target function has zero callers before allocating a
  mission to something nothing can reach.

CRITICAL: Do NOT use either tool to perform the audit yourself or look for security
vulnerabilities. Trust the semantic tagging metrics completely. Use them exclusively to
rule out empty implementation frameworks or unreachable code.

You have {len(candidates)} candidates spanning only {unique_files} unique files. Peek each
unique file at most once, total, for the whole session — the candidates are grouped by
file under <target_file> above specifically so one peek answers every candidate in that
group. A tool result stays valid for every candidate sharing that file; scroll up and
reuse it rather than re-issuing the same read_file call.
</investigation_capabilities>

<bundling_guidance>
Most missions are one candidate → one worker. Occasionally two candidates describe a
genuine two-part attack chain rather than two independent bugs — one candidate's flaw only
matters, or matters much more, because of the other. Descriptor authors signal this
directly in a candidate's `scenario` or `exploitation_context` text: phrases like "see X
for whether the source side ALSO has...", "assume Y works correctly", or an explicit
reference to another descriptor's id or name.

When you see this pattern AND both referenced candidates are present in this candidate
list, consider bundling them into ONE mission by listing both indices in
`candidate_indices`. State the dependency explicitly in `rationale` — which is upstream,
which is downstream, and why testing them together beats two isolated missions. The
bundled worker will verify the full chain end-to-end and return a single combined verdict.

Do not bundle on vibes or shared theme alone — only when the cross-reference is explicit
in the descriptor text, or `read_file`/`graph_query` gives you concrete evidence of a real
call-path dependency between the two contracts.
</bundling_guidance>

<output_format>
When ready, call `submit_missions` exactly once. Every single candidate index must appear exactly once across your selections.

Example final payload:
{{
  "missions": [
    {{
      "candidate_index": 0,
      "property_ids": [],
      "hints": [
        "SA-003 flags withdraw() at Vault.sol:120. Verified file contains matching active logic.",
        "Ensure states are balanced correctly when calling external targets."
      ],
      "rationale": "High semantic risk matching active, multi-layered structural logic."
    }}
  ],
  "skipped": [
    {{
      "candidate_index": 1,
      "reason": "Peeked code confirmed target source file is a pure interface containing zero executable paths."
    }}
  ]
}}
</output_format>

<task>
Bias toward creating missions. A missed investigation path is permanent, whereas a false positive only utilizes a single worker round.

Create a mission whenever an investigation is likely to produce useful security coverage.
Skip a candidate only when worker time would almost certainly be wasted (e.g., pure interfaces, completely abstract wrappers with zero implementation complexity, or empty mock targets).
</task>

<rules>
- Every candidate index must appear exactly once inside either `missions` or `skipped`.
- Keep rationales concise and straightforward.
- Hints should be concrete, utilizing metadata or peeking insights to orient workers immediately.
- Never issue a follow-up action after calling `submit_missions`.
</rules>"""


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
        semantic_tags=candidate.matching_tags,
        semantic_evidence=candidate.tag_evidence,
        detected_protocols=candidate.detected_protocols,
        documentation=doc,
        static_analysis=static,
        fork=fork,
        previous_audits=previous_audits,
        diff=diff,
        investigation_scope=InvestigationScope(
            contracts=[candidate.contract]
            + [c for c in related if c != candidate.contract],
            entrypoints=[],
            ignore=[],
        ),
    )


def _build_missions(
    raw_missions: list[dict],
    candidates: list[_Candidate],
    context: dict,
    step_name: str,
) -> list[Mission]:
    missions: list[Mission] = []
    for raw in raw_missions:
        idxs = raw.get("candidate_indices", [])
        valid_idxs = []
        for idx in idxs:
            if not isinstance(idx, int) or idx < 0 or idx >= len(candidates):
                default_debug_logger.log_event(
                    "coordinator_invalid_candidate_index",
                    step=step_name,
                    candidate_index=idx,
                    candidate_count=len(candidates),
                )
                continue
            valid_idxs.append(idx)

        if not valid_idxs:
            continue

        legs = []
        for idx in valid_idxs:
            candidate = candidates[idx]
            knowledge = _build_knowledge(context, candidate)
            legs.append(
                MissionLeg(
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
                    ],
                )
            )

        missions.append(
            Mission(
                id=f"M-{uuid.uuid4().hex[:32].upper()}",
                legs=legs,
                planner_rationale=raw.get("rationale", ""),
                shared_hints=raw.get("hints", []),
            )
        )

    return missions
