from __future__ import annotations

import json

from ignite_agent.steps.base import BaseStep


class FinalReportStep(BaseStep):
    name = "final_report"

    def select_context(self, global_context: dict) -> dict:
        return global_context

    def initial_prompt(self, context: dict) -> str:
        return f"""
You are producing the FINAL SECURITY AUDIT REPORT for a Web3 / EVM project.

Project path: {self.project_path}

You are given aggregated findings from previous analysis steps.

────────────────────────────────────────
HARD CONSTRAINT
────────────────────────────────────────
You MUST NOT:
- introduce new vulnerabilities
- provide generic audit checklists
- repeat standard security advice
- suggest broad “things to review”

You MUST ONLY work with:
- existing findings
- uncertainty in those findings
- contradictions or missing evidence

────────────────────────────────────────
CORE TASKS
────────────────────────────────────────

1. CONSOLIDATE FINDINGS
- Merge duplicates across tools
- Normalize severity:
  Critical / High / Medium / Low / Informational

2. PRESERVE TRACEABILITY
Every issue must remain traceable to original findings.
Never lose source attribution.

3. EXPLOIT CHAINING (DERIVED ONLY)
If and only if supported by existing findings:
- identify exploit chains
- link back to original issues explicitly
- DO NOT remove or hide original issues

4. UNCERTAINTY EXTRACTION (IMPORTANT NEW RULE)
Identify ONLY real ambiguity in reasoning, such as:

- conflicting tool outputs
- insufficient code visibility
- unclear exploitability conditions
- assumptions that were not verified
- partial evidence (e.g. only static analysis, no runtime confirmation)

DO NOT include:
- generic audit advice
- known best practices
- standard security reminders

Only include uncertainty that emerges from analysis.

Example GOOD uncertainty signals:
- "Exploitability depends on external contract behavior not observed"
- "Static analysis flags issue but source context incomplete"
- "Conflicting severity between detectors"

5. IMPACT SYNTHESIS
Summarize system-level risk based on confirmed findings only.

────────────────────────────────────────
OUTPUT FORMAT (STRICT JSON ONLY)
────────────────────────────────────────

{{
  "status": "complete",

  "findings": {{
    "critical": [],
    "high": [],
    "medium": [],
    "low": [],
    "informational": [],

    "exploit_chains": [
      {{
        "name": "...",
        "steps": [
          "Step referencing original finding A",
          "Step referencing original finding B"
        ],
        "linked_findings": ["static_analysis#5"],
        "final_impact": "...",
        "severity": "high"
      }}
    ],

    "uncertainty_flags": [
      {{
        "area": "specific contract/module/function",
        "reason": "why the analysis is uncertain",
        "type": "insufficient_evidence | conflicting_signals | unverified_assumption",
        "related_findings": ["static_analysis#3"]
      }}
    ],

    "summary_metrics": {{
      "total_issues": 0,
      "critical_count": 0,
      "high_count": 0,
      "medium_count": 0,
      "low_count": 0,
      "false_positive_estimate": "low|medium|high"
    }},

    "final_risk_rating": "LOW|MEDIUM|HIGH|CRITICAL"
  }},

  "summary": "Concise executive summary of validated risks and system-level security posture."
}}

────────────────────────────────────────
STRICT RULES
────────────────────────────────────────

- DO NOT generate generic advice (no checklists, no best practices)
- DO NOT suggest new areas to investigate unless uncertainty is explicitly derived
- ONLY surface uncertainty when evidence is missing, conflicting, or partial
- Preserve all original findings even when grouped into chains
- Chains do NOT replace findings
- Uncertainty must be grounded in observed limitations of prior analysis

───────────────
INPUT FROM PRIOR STEPS
───────────────

{json.dumps(context, indent=2)}
"""

    def validate_complete(self, findings: dict) -> bool:
        required = {
            "final_risk_rating",
            "critical",
            "high",
            "medium",
            "low",
            "informational",
            "exploit_chains",
            "uncertainty_flags",
        }

        return isinstance(findings, dict) and required.issubset(findings.keys())
