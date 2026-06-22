from __future__ import annotations

from ignite_agent.steps.base import BaseStep


class StaticAnalysisStep(BaseStep):
    name = "static_analysis"

    def select_context(self, global_context: dict) -> dict:
        return {}

    def initial_prompt(self, context: dict) -> str:
        return f"""You are starting a security audit.
Project path: {self.project_path}

Your task for this step:
  1. Run slither with JSON output to get all detector findings.
  2. Categorize each finding: high / medium / low / informational.
  3. For every HIGH or MEDIUM finding, read the affected source file to confirm
     it is a real issue and not a false positive.
  4. CRITICAL: If slither fails to execute due to compilation or framework setup errors,
     do not loop execution! Pivot immediately to manual file scanning using 'list_sol_files'
     and 'read_file' to locate vulnerability structures yourself.

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

Complete findings format — pass this shape as the `findings` argument to submit_findings:
  {{
    "high":          [{{"id": 1, "check": "...", "description": "...", "location": "...", "confirmed": true, "recommendation": "..."}}],
    "medium":        [...],
    "low":           [...],
    "informational": [...],
    "false_positives": [{{"check": "...", "reason": "..."}}]
  }}

Start by running: slither ./ --json -"""

    def validate_complete(self, findings: dict) -> bool:
        """Require at least one severity bucket or explicit warning fallback to be present."""
        return any(
            k in findings for k in ("high", "medium", "low", "informational", "warning")
        )
