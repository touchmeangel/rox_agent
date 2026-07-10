from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ignite_core.debug_logger import DebugLogger
from ignite_core.reporting import Reporter
from ignite_core.steps.base import BaseStep, StepResult
from ignite_core.tools import dispatch

_FORK_README_KEYWORDS = [
    "fork of",
    "forked from",
    "based on",
    "adapted from",
    "modified from",
    "originally from",
    "upstream:",
]

ForkConfidence = Literal["none", "possible", "likely"]


@dataclass
class ForkSignals:
    confidence: ForkConfidence
    remote_output: str = ""
    git_log_output: str = ""
    readme_hints: list[str] = field(default_factory=list)
    log_hints: list[str] = field(default_factory=list)


def _shell_prescan(
    command: str,
    project_path: str,
    reporter: Reporter,
    debug_logger: DebugLogger,
    max_chars: int = 2_000,
) -> str:
    result = dispatch(
        "shell",
        {"command": command},
        project_path,
        reporter=reporter,
        search_state=None,
        debug_logger=debug_logger,
    )
    out = (result.stdout or "").strip()
    return (
        (out[:max_chars] + "\n…(truncated)")
        if len(out) > max_chars
        else out or "(no output)"
    )


def _detect_fork_signals(
    github_repo_path: str,
    project_path: str,
    reporter: Reporter,
    debug_logger: DebugLogger,
    doc_summary: str,
) -> ForkSignals:
    remote_output = _shell_prescan(
        f"cd '{github_repo_path}' && git remote -v 2>/dev/null || echo '(no remotes)'",
        project_path,
        reporter,
        debug_logger,
    )
    has_upstream_remote = "upstream" in remote_output.lower()

    git_log_output = _shell_prescan(
        f"cd '{github_repo_path}' && git log --oneline -15 2>/dev/null || echo '(no history)'",
        project_path,
        reporter,
        debug_logger,
    )
    log_hints = [
        line.strip()
        for line in git_log_output.splitlines()
        if any(
            kw in line.lower()
            for kw in ["fork", "initial commit from", "based on", "upstream"]
        )
    ]

    readme_hints = [
        line.strip()
        for line in doc_summary.splitlines()
        if any(kw in line.lower() for kw in _FORK_README_KEYWORDS)
    ][:8]

    hard = has_upstream_remote
    soft = bool(log_hints or readme_hints)

    confidence: ForkConfidence = "likely" if hard else "possible" if soft else "none"

    return ForkSignals(
        confidence=confidence,
        remote_output=remote_output,
        git_log_output=git_log_output,
        readme_hints=readme_hints,
        log_hints=log_hints,
    )


def _fmt_signals(signals: ForkSignals) -> str:
    lines = [f"Confidence: {signals.confidence.upper()}"]

    if signals.readme_hints:
        lines.append("\nREADME hints:")
        lines.extend(f"  > {h}" for h in signals.readme_hints)

    if signals.log_hints:
        lines.append("\ngit log hints:")
        lines.extend(f"  > {h}" for h in signals.log_hints)

    lines.append(f"\ngit remote -v:\n{signals.remote_output}")
    lines.append(f"\ngit log (last 15):\n{signals.git_log_output}")

    return "\n".join(lines)


class ForkAnalysisStep(BaseStep):
    name = "fork_analysis"

    def run(self, context: dict) -> StepResult:
        self.project_path = self.github_repo_path
        return super().run(context)

    def select_context(self, global_context: dict) -> dict:
        return {"documentation": global_context.get("documentation", {})}

    def findings_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "is_fork": {"type": "boolean"},
                "upstream": {
                    "type": ["object", "null"],
                    "properties": {
                        "protocol": {"type": "string"},
                        "repo": {"type": "string"},
                        "base_commit": {"type": "string"},
                    },
                    "required": ["protocol", "repo", "base_commit"],
                },
                "modified_files": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "summary": {"type": "string"},
            },
            "required": ["is_fork", "upstream", "modified_files", "summary"],
        }

    def validate_complete(self, findings: dict) -> str | None:
        if not isinstance(findings, dict):
            return "findings must be json object (python dict)"
        
        if "is_fork" not in findings or not findings.get("summary"):
            return "missing 'is_fork' or 'summary'"
        if not findings["is_fork"]:
            return None
        upstream = findings.get("upstream")
        if (
            not upstream
            or not upstream.get("protocol")
            or not upstream.get("base_commit")
        ):
            return "upstream must have 'protocol' and 'base_commit'"
        return None

    def environment_prompt(self) -> str:
        return f"""- Repository path (read-only, full git history): {self.github_repo_path}
- ALL tool calls in this step run against the repository path above.
  Do NOT reference /work — it is irrelevant to fork detection."""

    def initial_prompt(
        self, context: dict, reporter: Reporter, debug_logger: DebugLogger
    ) -> str:
        doc_findings = context.get("documentation", {}).get("findings", {})
        doc_summary = "\n".join(
            filter(
                None,
                [
                    doc_findings.get("protocol_summary", ""),
                    doc_findings.get("architecture_notes", ""),
                ],
            )
        )

        signals = _detect_fork_signals(
            self.github_repo_path,
            self.project_path,
            reporter,
            debug_logger,
            doc_summary,
        )

        signals_section = _fmt_signals(signals)

        if signals.confidence == "none":
            strategy_block = (
                "No fork signals detected. Spend at most 1-2 iterations checking the git "
                "history and README. If nothing contradicts this, submit with is_fork=false."
            )
        elif signals.confidence == "possible":
            strategy_block = (
                "Soft signals found. Spend at most 4-5 iterations confirming. Check git log "
                "and README for any explicit fork mention, make some web_search requests. If "
                "you cannot confirm it is a fork, submit with is_fork=false — do not guess."
            )
        else:
            strategy_block = (
                "An upstream remote was detected, which is a strong signal. Verify it is a "
                "meaningful fork (not a personal mirror or template) before proceeding to the "
                "diff. If it turns out to be a mirror or boilerplate, submit with is_fork=false."
            )

        return f"""<audit_step name="fork_analysis">

<scope>
Determine whether this repository is a fork of a known protocol. This step does NOT
analyse vulnerabilities or confirm anything is exploitable — it only identifies what
changed relative to upstream. A later step and worker missions examine the diff for
issues. Do not write or reference exploit code in this step.
</scope>

<repository>
<repo_location>{self.github_repo_path}</repo_location>
<project_path>{self.project_path}</project_path>
</repository>

<pre_scan>
{signals_section}
</pre_scan>

<strategy>
{strategy_block}
</strategy>

<task>
<if_not_fork>
Confirm with a quick check and submit; web_search if the project's origin is unknown.
Two or three tool calls maximum.
</if_not_fork>

<if_fork>
<step id="1" title="Find the divergence point">
git log --oneline --all | tail -20
git tag --sort=-version:refname | head
</step>
<step id="2" title="Diff against that commit">
git diff base-commit HEAD -- src/
</step>
<step id="3" title="Collect and submit">
Collect the list of modified files and submit. Do not analyse the changes for
vulnerabilities. Do not search for audits. Do not read individual functions in
depth — that is scoped to later steps and worker missions. Just produce the file list.
</step>
</if_fork>
</task>

<output_format>
Not a fork:
{{
  "is_fork": false,
  "upstream": null,
  "modified_files": [],
  "summary": "Original codebase — no fork signals confirmed."
}}

Is a fork:
{{
  "is_fork": true,
  "upstream": {{
    "protocol": "Uniswap V2",
    "repo": "https://github.com/Uniswap/v2-core",
    "base_commit": "abc1234"
  }},
  "modified_files": [
    "src/Pair.sol",
    "src/Router.sol"
  ],
  "summary": "Fork of Uniswap V2 @ abc1234. Two files modified: Pair.sol, Router.sol."
}}
</output_format>

<rules>
<rule>Stop after producing the modified file list. Do not read file contents in depth.</rule>
<rule>Do not flag vulnerabilities or write exploit code — that is out of scope for this step.</rule>
<rule>If you cannot confirm a fork after 3 attempts, submit with is_fork=false and note the ambiguity in the summary.</rule>
<rule>A personal mirror, template repo, or boilerplate is NOT a fork.</rule>
</rules>

</audit_step>"""
