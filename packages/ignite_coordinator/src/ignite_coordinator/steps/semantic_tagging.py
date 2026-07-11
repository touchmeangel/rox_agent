from __future__ import annotations

from ignite_core.debug_logger import DebugLogger
from ignite_core.reporting import Reporter
from ignite_core.steps.base import BaseStep
from ignite_core.tools import list_sol_files
from ignite_knowledge.protocol_tags import load_protocol_tag_registry
from ignite_knowledge.tags import load_tag_registry

_CONTEXT_KEYS = ("documentation", "static_analysis", "test_coverage")


def _vuln_tag_ids() -> list[str]:
    return sorted(load_tag_registry().keys())


class SemanticTaggingStep(BaseStep):
    name = "semantic_tagging"

    def select_context(self, global_context: dict) -> dict:
        return {k: v for k, v in global_context.items() if k in _CONTEXT_KEYS}

    def findings_schema(self) -> dict:
        semantic_ids = _vuln_tag_ids()

        return {
            "type": "object",
            "required": [
                "semantic_tags",
                "tag_evidence",
                "detected_protocols",
            ],
            "properties": {
                "semantic_tags": {
                    "type": "object",
                    "description": (
                        "Map of contract path (relative to the project root) to the "
                        "semantic tags that describe architectural properties explicitly "
                        "present in that contract. Use {} if no tags apply."
                    ),
                    "additionalProperties": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": semantic_ids,
                        },
                    },
                },
                "tag_evidence": {
                    "type": "object",
                    "description": (
                        "Optional supporting evidence for every assigned semantic tag. "
                        "For each contract and tag, provide short factual observations "
                        "from the implementation that justify why the tag was assigned. "
                        "Reference function names, modifiers, variables, storage layout, "
                        "inheritance, imports, or other concrete implementation details. "
                        "Do not explain exploitability."
                    ),
                    "additionalProperties": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
                "detected_protocols": {
                    "type": "object",
                    "description": (
                        "Map of contract path (relative to the project root) to the "
                        "external protocols identified directly from imports, inherited "
                        "contracts, interfaces, or protocol-specific integrations."
                    ),
                    "additionalProperties": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        }

    def validate_complete(self, findings: dict) -> str | None:
        if not isinstance(findings, dict):
            return "findings must be json object (python dict)"

        valid_tag_ids = set(_vuln_tag_ids())

        semantic_tags = findings.get("semantic_tags")
        if semantic_tags is None:
            return (
                "findings.semantic_tags must be present "
                "(use {} if no semantic tags apply)"
            )

        tag_evidence = findings.get("tag_evidence")
        if tag_evidence is None:
            return (
                "findings.tag_evidence must be present "
                "(use {} if no semantic tags were assigned)"
            )

        detected_protocols = findings.get("detected_protocols")
        if detected_protocols is None:
            return (
                "findings.detected_protocols must be present "
                "(use {} if no external protocols are found)"
            )

        for contract, tags in semantic_tags.items():
            if not isinstance(tags, list):
                return f"semantic_tags['{contract}'] must be a list of semantic tag ids"

            for tag in tags:
                if tag not in valid_tag_ids:
                    return (
                        f"Unknown semantic tag '{tag}' for contract '{contract}'. "
                        f"Valid tags: {sorted(valid_tag_ids)}. "
                        "Do not invent new semantic tags."
                    )

            evidence = tag_evidence.get(contract, {})

            if not isinstance(evidence, dict):
                return f"tag_evidence['{contract}'] must be an object"

            for tag, observations in evidence.items():
                if tag not in tags:
                    return (
                        f"tag_evidence['{contract}']['{tag}'] exists but "
                        "that tag was not assigned in semantic_tags."
                    )

                if not isinstance(observations, list):
                    return (
                        f"tag_evidence['{contract}']['{tag}'] "
                        "must be a list of strings."
                    )

                if not observations:
                    return (
                        f"tag_evidence['{contract}']['{tag}'] "
                        "must contain at least one observation."
                    )

        for contract, protocols in detected_protocols.items():
            if not isinstance(protocols, list):
                return f"detected_protocols['{contract}'] must be a list of strings"

        return None

    def environment_prompt(self) -> str:
        base = super().environment_prompt()

        return (
            base
            + """

- Objective: Build a semantic index of the repository.

- For every Solidity contract, determine which semantic tags accurately describe
  architectural properties that are explicitly present in the implementation,
  and identify which external protocols the contract integrates with.

- Semantic tags describe WHAT a contract is or WHAT mechanisms it contains.
  They are architectural facts, not security conclusions.

- Do not assess exploitability, estimate severity, choose vulnerability
  descriptors, or decide which investigations should run later. Those are
  separate phases of the pipeline.

- Every assigned semantic tag should be supported by concrete observations from
  the implementation that can later be recorded as tag_evidence.
"""
        )

    def initial_prompt(
        self, context: dict, reporter: Reporter, debug_logger: DebugLogger
    ) -> str:
        vuln_registry = load_tag_registry()
        protocol_registry = load_protocol_tag_registry()

        vuln_tag_list = "\n".join(
            f"  - {tag_id}: {defn.description}"
            for tag_id, defn in sorted(vuln_registry.items())
        )
        protocol_tag_list = "\n".join(
            f"  - {tag_id}: {defn.description}"
            for tag_id, defn in sorted(protocol_registry.items())
        )

        contract_files = (
            "\n".join(self.repo_graph.files)
            if self.repo_graph
            else list_sol_files(self.project_path).stdout
        )

        return f"""<audit_step name="semantic_classification">

<scope>
Build a semantic index of this repository.

Your job is to describe WHAT each Solidity contract is and WHICH architectural
properties are explicitly present in its implementation. These semantic facts are
consumed by later planning stages to decide which vulnerability descriptors should
be investigated.

You are not performing security analysis. Do not assess exploitability, estimate
severity, or decide whether a vulnerability exists.
</scope>

<mental_model>
A semantic tag is an architectural fact about a contract, not a security judgment.

Examples:
- pooled_share_vault
- oracle_dependency
- upgradeable_proxy
- signature_authorized_action

These remain true regardless of whether the implementation is secure.

Assign tags only when they are directly supported by the implementation.
</mental_model>

<tag_vocabulary note="Use ONLY these ids in semantic_tags. Never invent new semantic tags.">
<semantic_tags>
{vuln_tag_list}
</semantic_tags>

<protocol_names note="Prefer these standard names when applicable. If a protocol is not listed, use its commonly accepted name.">
{protocol_tag_list}
</protocol_names>
</tag_vocabulary>

<pre_scan>
<contract_files note="Contracts have already been enumerated. Read these files directly; do not call list_sol_files again.">
{contract_files}
</contract_files>
</pre_scan>

<output_format>
Pass this to submit_findings once, for ALL contracts at once — not incrementally:

{{
  "semantic_tags": {{
    "src/Vault.sol": ["pooled_share_vault", "oracle_dependency"],
    "src/MathLib.sol": []
  }},

  "tag_evidence": {{
    "src/Vault.sol": {{
      "pooled_share_vault": [
        "deposit() mints shares via _convertToShares() using totalSupply/totalAssets ratio",
        "totalAssets tracked in a single storage variable shared across all depositors"
      ],
      "oracle_dependency": [
        "getPrice() calls IChainlinkAggregator(priceFeed).latestRoundData()",
        "liquidate() reverts if price staleness check in _validatePrice() fails"
      ]
    }}
  }},

  "detected_protocols": {{
    "src/Vault.sol": ["Chainlink"],
    "src/MathLib.sol": []
  }}
}}

Notes on the example above:
- Every contract in <contract_files> gets an entry in semantic_tags, even if the list is empty ([]).
- tag_evidence is only populated for contracts/tags that actually got tags — MathLib.sol has none, so it has no key in tag_evidence at all.
- Each evidence array has 1-3 short, concrete observations — not vague restatements of the tag name.
</output_format>

<task>
<step id="1" title="Read each contract">
For every Solidity contract:
1. Read the implementation.
2. Understand its architectural role.
3. Assign every semantic tag that is explicitly supported by the code.
4. For every assigned tag, record concise implementation evidence explaining why
the tag applies. Evidence should reference concrete implementation details such
as functions, modifiers, inheritance, storage layout, interfaces, imports, or
observable behaviors.
5. Identify every external protocol the contract integrates with by reading
imports, inherited contracts, interfaces, or protocol-specific APIs.
</step>

<step id="2" title="Submit">
Call submit_findings once with:

- semantic_tags
- tag_evidence
- detected_protocols

Use empty objects where appropriate.
</step>
</task>

<rules>
<rule>Semantic tags describe architecture, not vulnerabilities.</rule>
<rule>Assign a semantic tag only when supported by the implementation itself.</rule>
<rule>Every assigned semantic tag must have corresponding tag_evidence.</rule>
<rule>tag_evidence should contain short factual observations, not conclusions or speculation.</rule>
<rule>Determine protocol integrations from source code, not documentation or comments.</rule>
<rule>Do not investigate exploitability, write PoCs, describe attacks, or reason about whether something is vulnerable.</rule>
</rules>

</audit_step>"""
