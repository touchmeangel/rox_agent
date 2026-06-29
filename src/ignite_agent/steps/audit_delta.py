from __future__ import annotations

import re
from dataclasses import dataclass, field

from ignite_agent.debug_logger import DebugLogger
from ignite_agent.rolling_window import RollingWindow
from ignite_agent.search_state import SearchState
from ignite_agent.steps.base import BaseStep, StepResult
from ignite_agent.tools import dispatch


def _sh(
    command: str,
    project_path: str,
    window: RollingWindow,
    debug_logger: DebugLogger,
    max_chars: int = 3_000,
) -> str:
    result = dispatch(
        "shell",
        {"command": command},
        project_path,
        window=window,
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
    window: RollingWindow,
    debug_logger: DebugLogger,
) -> AuditPrescan:
    remote_url = _sh(
        f"cd '{repo_path}' && git remote get-url origin 2>/dev/null || echo '(no remote)'",
        project_path, window, debug_logger,
    )

    tags = _sh(
        f"cd '{repo_path}' && git tag --sort=-version:refname 2>/dev/null | head -20 || echo '(no tags)'",
        project_path, window, debug_logger,
    )

    git_log = _sh(
        f"cd '{repo_path}' && git log --oneline -25 2>/dev/null || echo '(no history)'",
        project_path, window, debug_logger,
    )

    audit_files = _sh(
        f"find '{repo_path}' -type f "
        r"\( -iname '*audit*' -o -iname '*review*' -o -iname '*security*' -o -iname '*assessment*' \) "
        r"-not -path '*/.git/*' -not -path '*/node_modules/*' -not -path '*/lib/*' "
        "2>/dev/null | head -20 || echo '(none found)'",
        project_path, window, debug_logger,
    )

    return AuditPrescan(
        remote_url=remote_url,
        tags=tags,
        git_log=git_log,
        audit_files=audit_files,
    )


def _fmt_prescan(scan: AuditPrescan) -> str:
    return "\n".join([
        "Clone depth:    Full clone — complete git history available.",
        f"\nRemote URL:     {scan.remote_url}",
        f"\nVersion tags:\n{scan.tags}",
        f"\nRecent git log:\n{scan.git_log}",
        f"\nAudit files found in repo:\n{scan.audit_files}",
    ])


_AUDIT_QUERIES = [
    "{name} security audit report",
    "{name} audit trail of bits certik code4rena",
]


def _prefetch_audit(
    protocol_name: str,
    project_path: str,
    window: RollingWindow,
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
            window=window,
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
                    "required": ["firm", "date", "url", "audited_commit", "audited_version"],
                },
                "diff_available": {"type": "boolean"},
                "changed_files": {"type": "array", "items": {"type": "string"}},
                "insertions": {"type": "integer"},
                "deletions": {"type": "integer"},
                "diff_excerpt": {"type": "string"},
                "risk_level": {"type": "string", "enum": ["none", "low", "medium", "high", "critical"]},
                "summary": {"type": "string"},
            },
            "required": [
                "audit_found", "audit", "diff_available", "changed_files",
                "insertions", "deletions", "diff_excerpt", "risk_level", "summary"
            ],
        }

    def validate_complete(self, findings: dict) -> str | None:
        if not isinstance(findings, dict):
            return "findings must be a dict"
        required = {
            "audit_found", "audit", "diff_available", "changed_files",
            "insertions", "deletions", "diff_excerpt", "risk_level", "summary",
        }
        missing = required - findings.keys()
        if missing:
            return f"missing fields: {', '.join(sorted(missing))}"
        if findings.get("audit_found") and not findings.get("audit"):
            return "audit_found is true but audit object is null"
        return None

    def initial_prompt(
        self, context: dict, window: RollingWindow, debug_logger: DebugLogger
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

        scan = _prescan(self.github_repo_path, self.project_path, window, debug_logger)
        prescan_section = _fmt_prescan(scan)

        prefetch_results = _prefetch_audit(
            protocol_name, self.project_path, window, debug_logger
        )
        prefetch_section = _fmt_prefetch(prefetch_results, protocol_name)

        return f"""You are running the audit delta step of a security audit.
Repository: {self.github_repo_path}

════════════════════════════════════════
PROTOCOL CONTEXT
════════════════════════════════════════
Protocol name: {protocol_name or '(unknown)'}
Summary: {protocol_summary}
Fork status: {fork_context}

════════════════════════════════════════
REPOSITORY SIGNALS
════════════════════════════════════════
{prescan_section}

════════════════════════════════════════
AUDIT PRE-FETCH RESULTS
════════════════════════════════════════
{prefetch_section}

════════════════════════════════════════
YOUR MANDATORY TASK STRATEGY
════════════════════════════════════════

You MUST execute BOTH tracks of verification to find historical security records. Do not stop at Track 1.

TRACK 1 — EXAMINE THE REPOSITORY INTERNALS
  1. Inspect any files matching audit keywords listed in REPOSITORY SIGNALS.
  2. Use `read_file` on the README.md, documentation directories, or configuration folders to discover mentions of previous audits or fixed commit targets.

TRACK 2 — EXAMINE EXTERNAL WEB SOURCES
  1. Actively parse the pre-fetched search results above. Use `web_fetch` to crawl any links that point to public markdown summaries, GitHub security publications, or audit registries.
  2. If the pre-fetch data falls short, execute explicit custom `web_search` queries targeting major platforms:
     - code4rena.com, audits.sherlock.xyz, spearbit.xyz, or github.com/trailofbits/publications

════════════════════════════════════════
DETERMINING THE BASELINE DIFF (CRITICAL)
════════════════════════════════════════

Once you discover details of an audit, your primary objective is to build a `git diff` comparing that audited state against the current `HEAD`. Run your analysis based on the quality of data found:

SCENARIO A: YOU FOUND THE EXACT COMMIT HASH OR TAG
  1. Generate the diff restricted strictly to source directories:
     `git diff <audited-commit> HEAD -- src/`

SCENARIO B: YOU ONLY FOUND AN AUDIT DATE / TIME PERIOD (NO COMMIT HASH)
  If the audit report only states a completion date (e.g., "Ended March 15, 2024"), calculate the closest historical commit matching that timestamp:
  1. Find the final commit before or on that date:
     `git log --before="2024-03-15" -n 1 --format="%H"`
  2. Use that returned hash as your baseline reference commit for your diff:
     `git diff <returned-hash> HEAD -- src/`
  3. Populate the `audited_commit` JSON field with that hash, and explain your date-based derivation in your `summary`.

SCENARIO C: NO AUDIT RECORDS EXIST
  If absolutely no audit context is uncovered across web queries or repository checks, use the root initialization commit as your fallback baseline:
  `git log --oneline | tail -1`

════════════════════════════════════════
RULES
════════════════════════════════════════
• `diff_excerpt` must feature raw code diff snippets (max ~3000 characters), not conversational text summaries.
• Focus entirely on core protocol code inside production directories (e.g., `src/`, `contracts/`). Disregard testing scripts, environment variables, or lockfiles.
• Do not invent data points. If a parameter cannot be accurately verified or derived via timestamp mapping, mark it as "unknown".
"""
