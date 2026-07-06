from __future__ import annotations

from ignite_core.debug_logger import DebugLogger
from ignite_core.reporting import Reporter
from ignite_core.steps.base import BaseStep
from ignite_core.tools import list_sol_files
from ignite_knowledge.tags import load_tag_registry

_CONTEXT_KEYS = ("documentation", "static_analysis", "test_coverage")


class TaggingStep(BaseStep):
    name = "tagging"

    def select_context(self, global_context: dict) -> dict:
        return {k: v for k, v in global_context.items() if k in _CONTEXT_KEYS}

    def findings_schema(self) -> dict:
        tag_ids = sorted(load_tag_registry().keys())
        return {
            "type": "object",
            "required": ["contract_tags"],
            "properties": {
                "contract_tags": {
                    "type": "object",
                    "description": (
                        "Map of contract path (relative to project root) to the "
                        "list of tags that apply to it. Use {} if no contract "
                        "matches anything in the vocabulary."
                    ),
                    "additionalProperties": {
                        "type": "array",
                        "items": {"type": "string", "enum": tag_ids},
                    },
                },
            },
        }

    def validate_complete(self, findings: dict) -> str | None:
        registry = load_tag_registry()

        contract_tags = findings.get("contract_tags")
        if contract_tags is None:
            return (
                "findings.contract_tags must be present "
                "(use {} if no contract matches any tag)"
            )

        for contract, tags in contract_tags.items():
            if not isinstance(tags, list):
                return f"contract_tags['{contract}'] must be a list of tag ids"
            for tag in tags:
                if tag not in registry:
                    return (
                        f"Unknown tag '{tag}' for contract '{contract}'. "
                        f"Valid tags: {sorted(registry.keys())}. "
                        "Do not invent new tags — if none of the existing tags "
                        "fit, omit tagging for that property rather than making "
                        "one up."
                    )


        return None

    def environment_prompt(self) -> str:
        base = super().environment_prompt()
        return base + (
            "\n\n- Objective Focus: You are a classification step, not an "
            "investigator. For each contract, decide which tags from the "
            "fixed vocabulary apply. Do NOT attempt to find, confirm, or rule "
            "out vulnerabilities — that happens in a later step with more "
            "context than you have here. Your only job is accurate, "
            "conservative tagging."
        )

    def initial_prompt(
        self, context: dict, reporter: Reporter, debug_logger: DebugLogger
    ) -> str:
        registry = load_tag_registry()
        tag_list = "\n".join(
            f"  - {tag_id}: {defn.description}"
            for tag_id, defn in sorted(registry.items())
        )

        def _summary(key: str) -> str:
            step = context.get(key, {})
            if not step or not step.get("success"):
                return "(not available)"
            return step.get("summary") or "(no summary)"

        contract_files = list_sol_files(self.project_path).stdout

        return f"""<audit_step name="tagging">

<scope>
Classify every contract in this repository against a fixed vocabulary of tags. These tags
decide which vulnerability checks are even relevant to each contract in a later step — get
this wrong and real checks get skipped, or irrelevant checks get run against contracts they
don't apply to. Getting tagging right is more valuable here than finding anything yourself.

You are a classification step, not an investigator: do not attempt to find, confirm, or
rule out vulnerabilities, and never write or reference exploit code. That happens
downstream, with more context than you have here.
</scope>

<tag_vocabulary note="use ONLY these ids, do not invent new ones">
{tag_list}
</tag_vocabulary>

<prior_context>
<documentation>{_summary("documentation")}</documentation>
<static_analysis>{_summary("static_analysis")}</static_analysis>
<test_coverage>{_summary("test_coverage")}</test_coverage>
</prior_context>

<pre_scan>
<contract_files note="already enumerated — no need to call list_sol_files yourself">
{contract_files}
</contract_files>
</pre_scan>

<task>
<step id="1" title="Read and classify each contract">
For each contract listed above, read it and decide which tags from the vocabulary above
apply. A contract can have zero, one, or several tags. Only tag what you can directly
observe in the code (an interface being implemented, a specific pattern being present) —
not what you suspect or guess might be true.
</step>

<step id="2" title="Submit">
Call submit_findings with contract_tags (map of relative file path to tag list). Use {{}} / [] if genuinely nothing applies — do not force tags onto a
contract to have something to report.
</step>
</task>

<rules>
<rule>Only use tag ids from the vocabulary above, verbatim. Any tag not in that exact list will be rejected and you'll need to resubmit.</rule>
<rule>Be conservative: a tag reflects something you directly observed, not a hunch. Under-tagging means a later step won't investigate something that turned out to be safe anyway. Over-tagging wastes real downstream investigation work on contracts that don't actually need it.</rule>
<rule>Never state or imply that something IS vulnerable. That is not this step's job and you do not have the context to judge it reliably — you are marking the presence of a pattern, nothing more.</rule>
<rule>Do not write, run, or reference exploit code, PoCs, or attack sequences in this step under any circumstance.</rule>
</rules>

</audit_step>"""
