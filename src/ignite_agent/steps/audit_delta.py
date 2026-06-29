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
    shallow: bool = False


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

    shallow_check = _sh(
        f"test -f '{repo_path}/.git/shallow' && echo 'shallow' || echo 'full'",
        project_path, window, debug_logger,
    )
    shallow = "shallow" in shallow_check

    return AuditPrescan(
        remote_url=remote_url,
        tags=tags,
        git_log=git_log,
        audit_files=audit_files,
        shallow=shallow,
    )


def _fmt_prescan(scan: AuditPrescan) -> str:
    depth_note = (
        "   Shallow clone detected (.git/shallow exists). "
        "If you need an older commit, run:\n"
        "  git fetch --unshallow\n"
        "or fetch a specific tag:\n"
        "  git fetch origin <tag> --depth=50"
        if scan.shallow
        else "Full clone — complete git history available."
    )

    return "\n".join([
        f"Clone depth:    {depth_note}",
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
            f"- The repo may be a shallow clone. Unshallow when necessary:\n"
            f"    git fetch --unshallow\n"
            f"    git fetch origin <tag>"
        )

    def findings_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "audit_found": {
                    "type": "boolean",
                    "description": "True if at least one public audit was found.",
                },
                "audit": {
                    "type": ["object", "null"],
                    "description": "Details of the most recent audit. Null if none found.",
                    "properties": {
                        "firm": {
                            "type": "string",
                            "description": "Auditing firm name, e.g. 'Trail of Bits'.",
                        },
                        "date": {
                            "type": "string",
                            "description": "Audit date or period, e.g. '2024-03'.",
                        },
                        "url": {
                            "type": "string",
                            "description": "URL to the audit report or announcement.",
                        },
                        "audited_commit": {
                            "type": "string",
                            "description": "Commit hash or tag the audit was performed on, or 'unknown'.",
                        },
                        "audited_version": {
                            "type": "string",
                            "description": "Version string, e.g. 'v1.2.0', or 'unknown'.",
                        },
                    },
                    "required": ["firm", "date", "url", "audited_commit", "audited_version"],
                },
                "diff_available": {
                    "type": "boolean",
                    "description": "True if a git diff against the audited commit was obtained.",
                },
                "changed_files": {
                    "type": "array",
                    "description": "In-scope source files that changed since the audit.",
                    "items": {"type": "string"},
                },
                "insertions": {
                    "type": "integer",
                    "description": "Total lines added since audit. 0 if diff not available.",
                },
                "deletions": {
                    "type": "integer",
                    "description": "Total lines removed since audit. 0 if diff not available.",
                },
                "diff_excerpt": {
                    "type": "string",
                    "description": (
                        "The most security-relevant portion of the diff — focus on logic "
                        "changes in core contracts. Max ~3000 chars. Empty string if diff "
                        "not available."
                    ),
                },
                "risk_level": {
                    "type": "string",
                    "enum": ["none", "low", "medium", "high", "critical"],
                    "description": (
                        "Risk introduced by post-audit changes. "
                        "none = no audit found or no changes. "
                        "low = test/comment/config changes only. "
                        "medium = non-critical logic tweaks. "
                        "high = core logic or access control changed. "
                        "critical = major rewrites or security-critical paths changed."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": "2-4 sentence summary of what was found and the risk.",
                },
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
            "audit_found", "audit", "diff_available", "changed_files",
            "insertions", "deletions", "diff_excerpt", "risk_level", "summary",
        }
        missing = required - findings.keys()
        if missing:
            return f"missing fields: {', '.join(sorted(missing))}"
        if not findings.get("summary"):
            return "summary is empty"
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

Protocol name (no name if none): {protocol_name or '(unknown)'}
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
YOUR TASK
════════════════════════════════════════

PHASE 1 — FIND THE MOST RECENT AUDIT
  Search for the latest public security audit of this protocol. Try in this order:

  1. Check repo first (fastest): read any audit files listed in REPOSITORY SIGNALS
     above, check README for audit links, check docs/ or audits/ directories.

  2. Use the pre-fetched results above: call web_fetch on any URL that looks like
     an audit report, announcement, or finding list to extract the audited
     commit/version. The pre-fetch already ran:
       • "{protocol_name or '<protocol name>'} security audit report"
       • "{protocol_name or '<protocol name>'} audit trail of bits certik code4rena"

  3. If pre-fetched results are insufficient, check known audit aggregators:
       - github.com/trailofbits/publications
       - github.com/spearbit/portfolio
       - audits.sherlock.xyz
       - code4rena.com (search for protocol name)
       - github.com/OpenZeppelin/openzeppelin-audits

  Target: firm name, date, URL, and — most importantly — the commit hash or
  version tag the audit was performed on.

PHASE 2 — DIFF AGAINST THE AUDITED VERSION
  Once you have the audited commit or tag:

  a. Check if it's in the local history:
       git log --oneline | grep <commit-prefix>
       git tag | grep <version>

  b. If not present (shallow clone), fetch it:
       git fetch origin <tag>
       OR: git fetch --unshallow   (use this only if no specific tag works)

  c. Compute the diff, scoped to source files:
       git diff <audited-commit> HEAD -- src/
       git diff --stat <audited-commit> HEAD -- src/

  d. If the audited commit cannot be determined or fetched:
     Use the oldest available commit as a fallback:
       git log --oneline | tail -1
       git diff <oldest-commit> HEAD -- src/
     Note this limitation in the summary.

PHASE 3 — ASSESS THE DIFF
  Focus only on in-scope source files (src/, contracts/ — not tests/, lib/).
  Extract the most security-relevant diff excerpt:
  - Access control changes (onlyOwner, roles, modifiers added/removed)
  - Logic changes in core financial functions (deposit, withdraw, swap, liquidate)
  - New external calls or integrations
  - Changes to storage layout or critical state variables
  Ignore: comment changes, whitespace, test files, dependency updates.

════════════════════════════════════════
FINDINGS FORMAT
════════════════════════════════════════

Audit found with diff:
{{
  "audit_found": true,
  "audit": {{
    "firm": "Trail of Bits",
    "date": "2024-02",
    "url": "https://github.com/trailofbits/publications/blob/master/...",
    "audited_commit": "a3f1b2c",
    "audited_version": "v1.2.0"
  }},
  "diff_available": true,
  "changed_files": ["src/Vault.sol", "src/Oracle.sol"],
  "insertions": 47,
  "deletions": 12,
  "diff_excerpt": "--- a/src/Vault.sol\\n+++ b/src/Vault.sol\\n@@ -142,7 +142,8 @@\\n ...",
  "risk_level": "high",
  "summary": "Trail of Bits audited v1.2.0 in Feb 2024. Since then, Vault.sol and Oracle.sol were modified with 47 insertions. The Oracle.sol changes alter the price feed source — this is unaudited and security-critical."
}}

No audit found:
{{
  "audit_found": false,
  "audit": null,
  "diff_available": false,
  "changed_files": [],
  "insertions": 0,
  "deletions": 0,
  "diff_excerpt": "",
  "risk_level": "none",
  "summary": "No public audit found after searching GitHub, Code4rena, Sherlock, and Trail of Bits publications. The entire codebase is unaudited."
}}

════════════════════════════════════════
RULES
════════════════════════════════════════

• Do NOT assess the diff for specific vulnerabilities — that is the static analysis step's job.
  Your job is only to find the audit and produce the delta.
• diff_excerpt must contain actual diff text, not a description of it.
  If the diff is large, include the most security-relevant hunks only (max ~3000 chars).
• risk_level is about the QUANTITY and NATURE of unaudited changes, not specific bugs.
• If you cannot find an audit after checking the pre-fetch results and 2-3 additional
  searches, submit with audit_found=false.
• Do not fabricate audit firms or commits — if unknown, use "unknown".
"""
