from __future__ import annotations

import json

from ignite_core.debug_logger import DebugLogger
from ignite_core.rolling_window import RollingWindow
from ignite_core.steps.base import BaseStep


class FinalReportStep(BaseStep):
    name = "final_report"

    def select_context(self, global_context: dict) -> dict:
        return global_context

    def findings_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "confirmed": {
                    "type": "array",
                    "description": "Deduplicated findings where the full exploit path is traceable through code.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "e.g., FR-001"},
                            "severity": {
                                "type": "string",
                                "enum": ["critical", "high", "medium", "low"],
                            },
                            "title": {"type": "string"},
                            "sources": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Upstream source finding IDs, e.g., ['static_analysis#SA-001'].",
                            },
                            "location": {
                                "type": "object",
                                "properties": {
                                    "file": {"type": "string"},
                                    "function": {"type": "string"},
                                    "lines": {"type": "string"},
                                },
                                "required": ["file", "function"],
                            },
                            "description": {"type": "string"},
                            "impact": {"type": "string"},
                            "recommendation": {"type": "string"},
                        },
                        "required": [
                            "id",
                            "severity",
                            "title",
                            "sources",
                            "location",
                            "description",
                            "impact",
                            "recommendation",
                        ],
                    },
                },
                "review_required": {
                    "type": "array",
                    "description": "Suspicious patterns requiring dynamic or manual tracking with a concrete blocking question.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "e.g., FR-R01"},
                            "estimated_severity": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                            "title": {"type": "string"},
                            "sources": {"type": "array", "items": {"type": "string"}},
                            "location": {
                                "type": "object",
                                "properties": {
                                    "file": {"type": "string"},
                                    "function": {"type": "string"},
                                },
                                "required": ["file", "function"],
                            },
                            "observation": {"type": "string"},
                            "blocking_question": {
                                "type": "string",
                                "description": "The single question whose answer determines exploitability.",
                            },
                            "how_to_verify": {
                                "type": "string",
                                "description": "Specific manual tracing, fuzzing property, or test execution directive.",
                            },
                        },
                        "required": [
                            "id",
                            "estimated_severity",
                            "title",
                            "sources",
                            "location",
                            "observation",
                            "blocking_question",
                            "how_to_verify",
                        ],
                    },
                },
                "informational": {
                    "type": "array",
                    "description": "Gas optimization flags, architectural notes, and non-exploitable code quality flags.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "e.g., FR-N01"},
                            "title": {"type": "string"},
                            "sources": {"type": "array", "items": {"type": "string"}},
                            "location": {
                                "type": "string",
                                "description": "File location string mapping, e.g., src/Token.sol:23",
                            },
                            "note": {"type": "string"},
                        },
                        "required": ["id", "title", "sources", "location", "note"],
                    },
                },
                "metrics": {
                    "type": "object",
                    "properties": {
                        "confirmed_critical": {"type": "integer", "minimum": 0},
                        "confirmed_high": {"type": "integer", "minimum": 0},
                        "confirmed_medium": {"type": "integer", "minimum": 0},
                        "confirmed_low": {"type": "integer", "minimum": 0},
                        "review_required_count": {"type": "integer", "minimum": 0},
                        "false_positive_estimate": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                        },
                    },
                    "required": [
                        "confirmed_critical",
                        "confirmed_high",
                        "confirmed_medium",
                        "confirmed_low",
                        "review_required_count",
                        "false_positive_estimate",
                    ],
                },
                "risk_rating": {
                    "type": "string",
                    "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"],
                    "description": "Overall deployment risk tier calculated strictly from confirmed findings.",
                },
            },
            "required": [
                "confirmed",
                "review_required",
                "informational",
                "metrics",
                "risk_rating",
            ],
        }

    def initial_prompt(
        self, context: dict, window: RollingWindow, debug_logger: DebugLogger
    ) -> str:
        return f"""You are producing the final security report for an EVM smart contract audit.

Your job is consolidation and triage — not new discovery.
All findings come from prior pipeline steps; organise them into a report that tells
a human auditor what to act on immediately and where to focus their manual research.

────────────────────────────────────────
INPUT FROM PRIOR STEPS
────────────────────────────────────────

{json.dumps(context, indent=2)}

════════════════════════════════════════
THREE TIERS
════════════════════════════════════════

CONFIRMED
  The full exploit path is traceable through code from the prior analysis.
  A reviewer can reproduce the issue from the evidence alone.
  This is a high bar. When in doubt, use REVIEW REQUIRED.

REVIEW REQUIRED
  A suspicious pattern was detected but exploitability cannot be determined
  from static analysis alone — runtime state, external contract behaviour,
  business-logic invariants, missing test coverage, etc.

  Each item MUST give the reviewer a specific investigation step, not a
  generic recommendation. The goal is to hand off a prioritised queue of
  concrete tasks, not a list of things to "look at".

    Good: "Call withdraw() after pool drain in the same tx to confirm the
           reentrancy window is reachable — check whether nonReentrant is
           actually applied to the outer entry point in MockVault.sol:88"
    Bad:  "Review reentrancy guards"

INFORMATIONAL
  Gas efficiency, style, non-exploitable observations. No immediate action needed.

════════════════════════════════════════
CONSOLIDATION RULES
════════════════════════════════════════

DEDUPLICATION
  If multiple tools flagged the same location for the same root cause,
  merge them into one finding and list all source IDs.

SEVERITY CALIBRATION
  • Escalate only when the context provides concrete additional evidence.
  • De-escalate freely: if a High finding is unreachable or depends on an
    unmet condition, drop it to Low or mark it a false positive with one
    clear reason.
  • Base confirmed severity on the worst realistic outcome, not theoretical
    maximum.

RISK RATING
  Reflects confirmed findings only. Review-required items do not inflate it.

TRACEABILITY
  Every finding must list its source IDs (e.g. "static_analysis#SA-001").
  Never discard a finding without an explicit reason.

CODE VERIFICATION
  You have read_file available. Use it when re-reading a specific code
  section would materially change a finding's tier or severity — resolving
  a conflict between tools, confirming a false-positive call, etc.
  Do not use it for general re-scanning; that already happened upstream.

════════════════════════════════════════
FINDINGS SCHEMA  (pass to submit_findings)
════════════════════════════════════════

{{
  "confirmed": [
    {{
      "id": "FR-001",
      "severity": "critical | high | medium | low",
      "title": "Short title",
      "sources": ["static_analysis#SA-001"],
      "location": {{
        "file": "src/Vault.sol",
        "function": "withdraw",
        "lines": "45-67"
      }},
      "description": "What the vulnerability is and how it is triggered",
      "impact": "What an attacker achieves and the blast radius",
      "recommendation": "Concrete fix"
    }}
  ],

  "review_required": [
    {{
      "id": "FR-R01",
      "estimated_severity": "high | medium | low",
      "title": "Short title",
      "sources": ["static_analysis#SA-S01"],
      "location": {{
        "file": "src/Oracle.sol",
        "function": "getPrice"
      }},
      "observation": "What was detected and why it looks suspicious",
      "blocking_question": "The single question whose answer determines exploitability",
      "how_to_verify": "Specific action: a test to write, a fuzzer property, a line to trace manually"
    }}
  ],

  "informational": [
    {{
      "id": "FR-N01",
      "title": "Short title",
      "sources": ["static_analysis#SA-N01"],
      "location": "src/Token.sol:23",
      "note": "One sentence"
    }}
  ],

  "metrics": {{
    "confirmed_critical": 0,
    "confirmed_high": 0,
    "confirmed_medium": 0,
    "confirmed_low": 0,
    "review_required_count": 0,
    "false_positive_estimate": "low | medium | high"
  }},

  "risk_rating": "LOW | MEDIUM | HIGH | CRITICAL"
}}

Pass a concise executive summary (3–5 sentences, confirmed findings only) as
the separate `summary` argument.

════════════════════════════════════════
STRICT RULES
════════════════════════════════════════

• Do not introduce findings that have no source in the prior steps' output.
• Do not add generic security advice, checklists, or best-practice reminders.
• review_required entries without a concrete blocking_question and how_to_verify
  are not acceptable — if you cannot write a specific verification step,
  drop the item or fold it into informational.
• False positives must be dismissed with one factual sentence, not omitted silently.
"""

    def validate_complete(self, findings: dict) -> str | None:
        if not isinstance(findings, dict):
            return "the output should be json object (python dict)"

        required = {
            "confirmed",
            "review_required",
            "informational",
            "metrics",
            "risk_rating",
        }
        if not required.issubset(findings.keys()):
            missing = required - findings.keys()
            return f"some of the required fields are missing ({', '.join(missing)})"

        if not isinstance(findings.get("confirmed"), list):
            return "invalid confirmed, must be a defined list"

        if not isinstance(findings.get("review_required"), list):
            return "invalid review_required, must be a defined list"

        if not isinstance(findings.get("informational"), list):
            return "invalid informational, must be a defined list"

        if not isinstance(findings.get("metrics"), dict):
            return "invalid metrics, must be an object (python dict)"

        if not findings.get("risk_rating"):
            return "risk_rating is not set which is required"

        return None
