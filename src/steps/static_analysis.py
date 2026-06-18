"""
Step 1 — Static analysis.

Runs `slither ./` and iteratively investigates findings.
This is the worked example. The other steps follow the same pattern.

The model will:
  1.  Run slither (JSON output)
  2.  For each high/medium finding, optionally read the source file
  3.  Filter false positives
  4.  Emit structured findings
"""

from __future__ import annotations

from steps.base import BaseStep


class StaticAnalysisStep(BaseStep):
    name = "static_analysis"

    def initial_prompt(self, context: dict) -> str:
        return f"""You are starting a security audit.
Project path: {self.project_path}

Your task for this step:
  1. Run slither with JSON output to get all detector findings.
  2. Categorize each finding: high / medium / low / informational.
  3. For every HIGH or MEDIUM finding, read the affected source file to confirm
     it is a real issue and not a false positive.
  4. When you have enough information, mark the step complete.

The slither JSON schema looks like:
  {{
    "success": true,
    "results": {{
      "detectors": [
        {{
          "check":       "reentrancy-eth",
          "impact":      "High",
          "confidence":  "Medium",
          "description": "...",
          "elements":    [{{ "name": "...", "source_mapping": {{ "filename_short": "..." }} }}]
        }}
      ]
    }}
  }}

Complete findings format:
  {{
    "status": "complete",
    "findings": {{
      "high":          [{{"id": 1, "check": "...", "description": "...", "location": "...", "confirmed": true, "recommendation": "..."}}],
      "medium":        [...],
      "low":           [...],
      "informational": [...],
      "false_positives": [{{"check": "...", "reason": "..."}}]
    }},
    "summary": "..."
  }}

Start by running: slither ./ --json -"""

    def validate_complete(self, findings: dict) -> bool:
        """Require at least one severity bucket to be present."""
        return any(k in findings for k in ("high", "medium", "low", "informational"))
