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


class TaggingStep(BaseStep):
    name = "tagging"

    def select_context(self, global_context: dict) -> dict:
        return {k: v for k, v in global_context.items() if k in _CONTEXT_KEYS}

    def findings_schema(self) -> dict:
        vuln_ids = _vuln_tag_ids()
        return {
            "type": "object",
            "required": ["contract_tags", "detected_protocols"],
            "properties": {
                "contract_tags": {
                    "type": "object",
                    "description": (
                        "Map of contract path (relative to project root) to the "
                        "list of vulnerability tags that apply to it. Use {} if no "
                        "contract matches anything in the vocabulary."
                    ),
                    "additionalProperties": {
                        "type": "array",
                        "items": {"type": "string", "enum": vuln_ids},
                    },
                },
                "detected_protocols": {
                    "type": "object",
                    "description": (
                        "Map of contract path (relative to project root) to the "
                        "list of external protocols identified directly in its "
                        "source code (via imports, interfaces, or inheritance)."
                    ),
                    "additionalProperties": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        }

    def validate_complete(self, findings: dict) -> str | None:
        valid_vuln_ids = set(_vuln_tag_ids())

        contract_tags = findings.get("contract_tags")
        if contract_tags is None:
            return (
                "findings.contract_tags must be present "
                "(use {} if no contract matches any tag)"
            )

        detected_protocols = findings.get("detected_protocols")
        if detected_protocols is None:
            return (
                "findings.detected_protocols must be present "
                "(use {} if no external protocols are found)"
            )

        for contract, tags in contract_tags.items():
            if not isinstance(tags, list):
                return f"contract_tags['{contract}'] must be a list of tag ids"
            for tag in tags:
                if tag not in valid_vuln_ids:
                    return (
                        f"Unknown vulnerability tag '{tag}' for contract '{contract}'. "
                        f"Valid structural tags: {sorted(valid_vuln_ids)}. "
                        "Do not invent new vulnerability tags — if none fit, omit it."
                    )

        for contract, protocols in detected_protocols.items():
            if not isinstance(protocols, list):
                return f"detected_protocols['{contract}'] must be a list of strings"

        return None

    def environment_prompt(self) -> str:
        base = super().environment_prompt()
        return base + (
            "\n\n- Objective Focus: You are a classification step, not an "
            "investigator. For each contract, decide which vulnerability tags apply "
            "and list the external protocols it interacts with. Do NOT attempt to "
            "find, confirm, or rule out vulnerabilities — that happens in a later step."
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

        contract_files = list_sol_files(self.project_path).stdout

        return f"""<audit_step name="tagging">

<scope>
Classify every contract in this repository against a fixed vocabulary of structural tags, 
and extract external protocol dependencies directly from the source code. These signals 
decide which downstream vulnerability checks are executed against each contract.

You are a classification step, not an investigator: do not attempt to find, confirm, or
rule out vulnerabilities, and never write or reference exploit code.
</scope>

<tag_vocabulary note="use ONLY these ids for contract_tags, do not invent new ones">
<vulnerability_pattern_tags note="a structural code pattern, not a judgment about severity or exploitability">
{vuln_tag_list}
</vulnerability_pattern_tags>

<suggested_protocol_names note="Use these standard names in detected_protocols if you match them, otherwise type the name of the protocol found in the imports">
{protocol_tag_list}
</suggested_protocol_names>
</tag_vocabulary>

<pre_scan>
<contract_files note="already enumerated — no need to call list_sol_files yourself">
{contract_files}
</contract_files>
</pre_scan>

<task>
<step id="1" title="Read and classify each contract">
For each contract listed above, open and read its source code to extract two sets of metadata:

1. **Vulnerability Tags (`contract_tags`)**: Identify matching structural code patterns from the `<vulnerability_pattern_tags>` vocabulary. Do NOT invent new tags here.
2. **External Protocols (`detected_protocols`)**: Identify every external protocol this contract interacts with by reading its `import` statements, contract inheritance, or interface usage (e.g., Uniswap, Chainlink, Aave, Compound). You may use names from `<suggested_protocol_names>` or type the name directly if it's missing from the vocabulary.
</step>

<step id="2" title="Submit">
Call submit_findings with both `contract_tags` and `detected_protocols`. Use empty objects if nothing applies.
</step>
</task>

<rules>
<rule>Vulnerability tags inside contract_tags must strictly match the vocabulary provided. Custom or invented vulnerability tags will be rejected.</rule>
<rule>detected_protocols is open-ended. Identify third-party systems directly via imports or interfaces in the code. Do NOT rely on documentation or READMEs; read the source files.</rule>
<rule>Never state or imply that something IS vulnerable. You are marking the presence of a pattern/integration, nothing more.</rule>
<rule>Do not write, run, or reference exploit code, PoCs, or attack sequences in this step under any circumstance.</rule>
</rules>

</audit_step>"""
