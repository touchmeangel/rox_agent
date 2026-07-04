from __future__ import annotations

from ignite_core.debug_logger import DebugLogger
from ignite_core.reporting import Reporter
from ignite_core.steps.base import BaseStep
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
            "required": ["contract_tags", "repo_tags"],
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

        return f"""# Tagging Step

Classify every contract in this repository against a FIXED vocabulary of
tags. These tags decide which vulnerability checks are even relevant to each
contract in a later step — get this wrong and real checks get skipped, or
irrelevant checks get run against contracts they don't apply to. Getting
tagging RIGHT is more valuable here than finding anything yourself.

## Fixed tag vocabulary — use ONLY these, do not invent new ones

{tag_list}

## Context from prior steps

Documentation: {_summary("documentation")}
Static analysis: {_summary("static_analysis")}
Test coverage: {_summary("test_coverage")}

## Workflow

1. list_sol_files to enumerate every contract in the project.
2. For each contract, read it and decide which tags from the vocabulary
   above apply. A contract can have zero, one, or several tags. Only tag
   what you can directly observe in the code (an interface being
   implemented, a specific pattern being present) — not what you suspect
   or guess might be true.
3. Identify any repo_tags that apply at the whole-codebase level rather
   than to one file.
4. Call submit_findings with contract_tags (map of relative file path to
   tag list) and repo_tags (list). Use {{}} / [] if genuinely nothing applies
   — do not force tags onto a contract to have something to report.

## Hard rules

- Only use tag ids from the vocabulary above, verbatim. Any tag not in
  that exact list will be rejected and you'll need to resubmit.
- Be conservative: a tag reflects something you directly observed, not a
  hunch. Under-tagging means a later step won't investigate something that
  turned out to be safe anyway. Over-tagging wastes real downstream
  investigation work on contracts that don't actually need it.
- Never state or imply that something IS vulnerable. That is not this
  step's job and you do not have the context to judge it reliably — you
  are marking the presence of a pattern, nothing more."""
