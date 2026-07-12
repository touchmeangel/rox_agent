from __future__ import annotations

from dataclasses import dataclass

from ignite_core.debug_logger import DebugLogger
from ignite_core.graph import Graph
from ignite_core.reporting import Reporter
from ignite_core.search_state import SearchState
from ignite_core.steps.base import BaseStep, StepResult
from ignite_core.tools import dispatch


def _sh(
    command: str,
    project_path: str,
    repo_graph: Graph,
    reporter: Reporter,
    debug_logger: DebugLogger,
    max_chars: int = 3_000,
) -> str:
    result = dispatch(
        "shell",
        {"command": command},
        project_path,
        repo_graph,
        reporter=reporter,
        search_state=None,
        debug_logger=debug_logger,
    )
    out = (result.stdout or "").strip()
    if len(out) > max_chars:
        return out[:max_chars] + "\n…(truncated)"
    return out or "(no output)"


@dataclass
class AuditPrescan:
    remote_url: str = ""
    tags: str = ""
    git_log: str = ""
    audit_files: str = ""


def _prescan(
    repo_path: str,
    project_path: str,
    repo_graph: Graph,
    reporter: Reporter,
    debug_logger: DebugLogger,
) -> AuditPrescan:
    remote_url = _sh(
        f"cd '{repo_path}' && git remote get-url origin 2>/dev/null || echo '(no remote)'",
        project_path,
        repo_graph,
        reporter,
        debug_logger,
    )

    tags = _sh(
        f"cd '{repo_path}' && git tag --sort=-version:refname 2>/dev/null | head -20 || echo '(no tags)'",
        project_path,
        repo_graph,
        reporter,
        debug_logger,
    )

    git_log = _sh(
        f"cd '{repo_path}' && git log --oneline -25 2>/dev/null || echo '(no history)'",
        project_path,
        repo_graph,
        reporter,
        debug_logger,
    )

    audit_files = _sh(
        f"find '{repo_path}' -type f "
        r"\( -iname '*audit*' -o -iname '*review*' -o -iname '*security*' -o -iname '*assessment*' \) "
        r"-not -path '*/.git/*' -not -path '*/node_modules/*' -not -path '*/lib/*' "
        "2>/dev/null | head -20 || echo '(none found)'",
        project_path,
        repo_graph,
        reporter,
        debug_logger,
    )

    return AuditPrescan(
        remote_url=remote_url,
        tags=tags,
        git_log=git_log,
        audit_files=audit_files,
    )


def _fmt_prescan(scan: AuditPrescan) -> str:
    return "\n".join(
        [
            "Clone depth:    Full clone — complete git history available.",
            f"\nRemote URL:     {scan.remote_url}",
            f"\nVersion tags:\n{scan.tags}",
            f"\nRecent git log:\n{scan.git_log}",
            f"\nAudit files found in repo:\n{scan.audit_files}",
        ]
    )


_AUDIT_QUERIES = [
    "{name} security audit report",
    "{name} audit trail of bits certik code4rena",
]


def _prefetch_audit(
    protocol_name: str,
    project_path: str,
    repo_graph: Graph,
    reporter: Reporter,
    debug_logger: DebugLogger,
    max_chars_per_result: int = 1_500,
) -> list[str]:
    if not protocol_name:
        return []

    results: list[str] = []
    search_state = SearchState(max_calls=len(_AUDIT_QUERIES))

    for template in _AUDIT_QUERIES:
        query = template.format(name=protocol_name)
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
            results.append(f"Query: {query}\n{out[:max_chars_per_result]}")

    return results


def _fmt_prefetch(results: list[str], protocol_name: str) -> str:
    if not protocol_name:
        return "(Protocol name unknown — audit pre-fetch skipped. Use web_search manually in Phase 1.)"
    if not results:
        return f"(No results returned for '{protocol_name}' — proceed with manual searches in Phase 1.)"
    count = len(results)
    header = f"Pre-fetched {count} result(s) for '{protocol_name}'. Use web_fetch on any promising URLs."
    body = "\n\n────────────────────────────────────\n\n".join(results)
    return f"{header}\n\n{body}"


class AuditDeltaStep(BaseStep):
    name = "audit_delta"

    def run(self, context: dict) -> StepResult:
        self.project_path = self.github_repo_path
        return super().run(context)

    def select_context(self, global_context: dict) -> dict:
        return {
            "documentation": global_context.get("documentation", {}),
            "fork_analysis": global_context.get("fork_analysis", {}),
        }

    def environment_prompt(self) -> str:
        return (
            f"- Repository (read-only, git history): {self.github_repo_path}\n"
            f"- All tool calls in this step run against the repository path above.\n"
            f"- Do NOT reference the workspace path — it is irrelevant here.\n"
        )

    def findings_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "audit_found": {"type": "boolean"},
                "audit": {
                    "type": ["object", "null"],
                    "properties": {
                        "firm": {"type": "string"},
                        "date": {"type": "string"},
                        "url": {"type": "string"},
                        "audited_commit": {"type": "string"},
                        "audited_version": {"type": "string"},
                    },
                    "required": [
                        "firm",
                        "date",
                        "url",
                        "audited_commit",
                        "audited_version",
                    ],
                },
                "diff_available": {"type": "boolean"},
                "changed_files": {"type": "array", "items": {"type": "string"}},
                "insertions": {"type": "integer"},
                "deletions": {"type": "integer"},
                "diff_excerpt": {"type": "string"},
                "risk_level": {
                    "type": "string",
                    "enum": ["none", "low", "medium", "high", "critical"],
                },
                "summary": {"type": "string"},
            },
            "required": [
                "audit_found",
                "audit",
                "diff_available",
                "changed_files",
                "insertions",
                "deletions",
                "diff_excerpt",
                "risk_level",
                "summary",
            ],
        }

    def validate_complete(self, findings: dict) -> str | None:
        if not isinstance(findings, dict):
            return "findings must be a dict"

        required = {
            "audit_found",
            "audit",
            "diff_available",
            "changed_files",
            "insertions",
            "deletions",
            "diff_excerpt",
            "risk_level",
            "summary",
        }
        missing = required - findings.keys()
        if missing:
            return f"missing fields: {', '.join(sorted(missing))}"
        if findings.get("audit_found") and not findings.get("audit"):
            return "audit_found is true but audit object is null"
        return None

    def initial_prompt(
        self, context: dict, reporter: Reporter, debug_logger: DebugLogger
    ) -> str:
        doc_findings = context.get("documentation", {}).get("findings", {})
        protocol_summary = doc_findings.get("protocol_summary", "(not available)")
        protocol_name = doc_findings.get("protocol_name") or ""

        fork_findings = context.get("fork_analysis", {}).get("findings", {})
        is_fork = fork_findings.get("is_fork", False)
        fork_upstream = fork_findings.get("upstream") or {}
        fork_context = (
            f"This repo is a fork of {fork_upstream.get('protocol', 'unknown')} "
            f"@ {fork_upstream.get('base_commit', 'unknown')}. "
            f"Search for audits of BOTH the upstream AND this fork."
            if is_fork
            else "This appears to be an original protocol."
        )

        scan = _prescan(
            self.github_repo_path,
            self.project_path,
            self.repo_graph,
            reporter,
            debug_logger,
        )
        prescan_section = _fmt_prescan(scan)

        prefetch_results = _prefetch_audit(
            protocol_name, self.project_path, self.repo_graph, reporter, debug_logger
        )
        prefetch_section = _fmt_prefetch(prefetch_results, protocol_name)

        return f"""<audit_step name="audit_delta">

<scope>
Find any prior audit of this protocol (or its fork upstream) and build a diff between the
audited state and the current HEAD. This step identifies what changed since the last known
review — it does not itself judge whether the changes are vulnerable. That assessment
happens in later steps and worker missions, using the diff you produce here. Do not write
or reference exploit code in this step.
</scope>

<repository>{self.github_repo_path}</repository>

<protocol_context>
<protocol_name>{protocol_name or "(unknown)"}</protocol_name>
<summary>{protocol_summary}</summary>
<fork_status>{fork_context}</fork_status>
</protocol_context>

<pre_scan>
{prescan_section}
</pre_scan>

<audit_prefetch>
{prefetch_section}
</audit_prefetch>

<output_format>
Pass this to submit_findings once:

{{
  "audit_found": true,
  "audit": {{
    "firm": "Trail of Bits",
    "date": "2024-03-15",
    "url": "https://github.com/trailofbits/publications/blob/master/reviews/2024-03-protocolx.pdf",
    "audited_commit": "a1b2c3d4e5f6...",
    "audited_version": "v1.2.0"
  }},
  "diff_available": true,
  "changed_files": ["src/Vault.sol", "src/Oracle.sol"],
  "insertions": 42,
  "deletions": 11,
  "diff_excerpt": "diff --git a/src/Vault.sol b/src/Vault.sol\\n@@ -120,7 +120,7 @@ function withdraw(...) {{\\n-    uint256 fee = amount * FEE_BPS / 10000;\\n+    uint256 fee = amount * FEE_BPS / 10_000;\\n...(raw diff content, max ~3000 chars, not prose)",
  "risk_level": "medium",
  "summary": "Trail of Bits audited commit a1b2c3d at v1.2.0 in March 2024. Since then, 2 files changed (42 insertions, 11 deletions), concentrated in Vault.sol's fee calculation and Oracle.sol's staleness check."
}}

If no audit is found (Scenario C), use the fallback baseline commit and set:
{{
  "audit_found": false,
  "audit": null,
  "diff_available": true,
  ...
  "risk_level": "none",
  "summary": "No prior audit found via repo scan or web search (queries: ...). Diffing against root commit <hash> as fallback baseline."
}}
</output_format>

<task>
You MUST execute BOTH tracks below to find historical security records. Do not stop at track 1.

<track id="1" title="Examine the repository internals">
1. Inspect any files matching audit keywords listed in the pre-scan above.
2. Use read_file on the README.md, documentation directories, or configuration folders to
   discover mentions of previous audits or fixed commit targets.
</track>

<track id="2" title="Examine external web sources">
1. Actively parse the pre-fetched search results above. Use web_fetch to crawl any links
   that point to public markdown summaries, GitHub security publications, or audit registries.
2. If the pre-fetch data falls short, execute explicit custom web_search queries targeting
   major platforms: code4rena.com, audits.sherlock.xyz, spearbit.xyz, or
   github.com/trailofbits/publications.
</track>

<determining_the_baseline_diff note="critical">
Once you discover details of an audit, your primary objective is to build a git diff
comparing that audited state against the current HEAD.

<scenario id="A" title="You found the exact commit hash or tag">
Generate the diff restricted strictly to source directories:
  git diff audited-commit HEAD -- src/
</scenario>

<scenario id="B" title="You only found an audit date (no commit hash)">
If the audit report only states a completion date (e.g., "Ended March 15, 2024"), calculate
the closest historical commit matching that timestamp:
  1. git log --before="2024-03-15" -n 1 --format="%H"
  2. Use that returned hash as your baseline: git diff returned-hash HEAD -- src/
  3. Populate audited_commit with that hash, and explain your date-based derivation in summary.
</scenario>

<scenario id="C" title="No audit records exist">
If absolutely no audit context is uncovered across web queries or repository checks, use the
root initialization commit as your fallback baseline:
  git log --oneline | tail -1
</scenario>
</determining_the_baseline_diff>
</task>

<rules>
<rule>diff_excerpt must feature raw code diff snippets (max ~3000 characters), not conversational text summaries.</rule>
<rule>Focus entirely on core protocol code inside production directories (e.g., src/, contracts/). Disregard testing scripts, environment variables, or lockfiles.</rule>
<rule>Do not invent data points. If a parameter cannot be accurately verified or derived via timestamp mapping, mark it as "unknown".</rule>
<rule>Do not assess whether the diffed changes are vulnerable, and do not write or reference exploit code — that is out of scope for this step.</rule>
</rules>

</audit_step>"""
