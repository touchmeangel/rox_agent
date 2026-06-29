from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ignite_agent.debug_logger import DebugLogger
from ignite_agent.rolling_window import RollingWindow
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
class TestSuitePrescan:
    has_foundry_toml: bool = False
    foundry_toml_content: str = ""
    test_files_list: str = ""
    test_files_count: int = 0


def _prescan_tests(workspace_path: str) -> TestSuitePrescan:
    base_path = Path(workspace_path)
    foundry_toml_path = base_path / "foundry.toml"
    has_toml = foundry_toml_path.exists()
    
    toml_content = ""
    if has_toml:
        try:
            toml_content = foundry_toml_path.read_text(errors="ignore")[:1000]
        except Exception:
            toml_content = "(error reading foundry.toml)"

    test_files = []
    try:
        for p in base_path.rglob("*.sol"):
            if any(x in p.parts for x in (".git", "node_modules", "lib")):
                continue
            
            name = p.name
            if name.endswith(".t.sol") or "test" in name.lower():
                test_files.append(str(p.relative_to(base_path)))
                if len(test_files) >= 30:
                    break
    except Exception:
        pass

    return TestSuitePrescan(
        has_foundry_toml=has_toml,
        foundry_toml_content=toml_content or "(empty or not readable)",
        test_files_list="\n".join(test_files) if test_files else "(none found)",
        test_files_count=len(test_files),
    )


class TestCoverageStep(BaseStep):
    name = "test_coverage"

    def run(self, context: dict) -> StepResult:        
        self.scan = _prescan_tests(self.project_path)
        
        if self.scan.test_files_count == 0:
            return StepResult(
                step_name=self.name,
                success=True,
                findings={
                    "has_tests": False,
                    "coverage_run_success": False,
                    "global_metrics": None,
                    "per_file_coverage": [],
                    "suite_anomalies": ["No local test suite detected in workspace."],
                    "summary": "Workspace compilation skipped: No explicit test targets (*.t.sol) were found outside of base libraries.",
                },
                summary="No local test files found — step skipped.",
                iterations_used=0,
            )
            
        return super().run(context)

    def select_context(self, global_context: dict) -> dict:
        return {
            "documentation": global_context.get("documentation", {}),
        }

    def findings_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "has_tests": {
                    "type": "boolean",
                    "description": "True if test targets or suites were detected in the workspace.",
                },
                "coverage_run_success": {
                    "type": "boolean",
                    "description": "True if forge coverage compiled and executed without a total runner crash.",
                },
                "global_metrics": {
                    "type": ["object", "null"],
                    "description": "Overall suite summary. Null if no tests exist.",
                    "properties": {
                        "total_lines": {"type": "integer"},
                        "covered_lines": {"type": "integer"},
                        "coverage_percentage": {"type": "number", "description": "Global percentage (0.0 to 100.0)"},
                    },
                    "required": ["total_lines", "covered_lines", "coverage_percentage"],
                },
                "per_file_coverage": {
                    "type": "array",
                    "description": "Detailed breakdown per in-scope production contract file (do not include test contracts).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "Relative path to target file, e.g., src/Vault.sol"},
                            "covered_lines": {"type": "integer"},
                            "total_lines": {"type": "integer"},
                            "percentage": {"type": "number"},
                            "silent_failure_detected": {
                                "type": "boolean",
                                "description": "True if individual assertions failed silently or execution logs reveal reverted edge-cases for this unit.",
                            },
                            "uncovered_regions": {
                                "type": "array",
                                "description": "List of notable functions, branches, or line ranges explicitly skipped or uncovered.",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["file_path", "covered_lines", "total_lines", "percentage", "silent_failure_detected", "uncovered_regions"],
                    },
                },
                "suite_anomalies": {
                    "type": "array",
                    "description": "Any silent failures, compilation warnings, missing coverage fixtures, or broken assertions found during audit.",
                    "items": {"type": "string"},
                },
                "summary": {
                    "type": "string",
                    "description": "A 2-4 sentence engineering summary of total suite execution safety and quantitative code coverage validation status.",
                },
            },
            "required": [
                "has_tests",
                "coverage_run_success",
                "global_metrics",
                "per_file_coverage",
                "suite_anomalies",
                "summary",
            ],
        }

    def validate_complete(self, findings: dict) -> str | None:
        if not isinstance(findings, dict):
            return "findings must be a dict"
        required = {
            "has_tests", "coverage_run_success", "global_metrics",
            "per_file_coverage", "suite_anomalies", "summary"
        }
        missing = required - findings.keys()
        if missing:
            return f"missing fields: {', '.join(sorted(missing))}"
        if findings.get("has_tests") and not findings.get("global_metrics"):
            return "has_tests is true but global_metrics object is null"
        if not findings.get("summary"):
            return "summary statement is empty"
        return None

    def initial_prompt(
        self, context: dict, window: RollingWindow, debug_logger: DebugLogger
    ) -> str:
        doc_findings = context.get("documentation", {}).get("findings", {})
        protocol_name = doc_findings.get("protocol_name") or ""
        contract_risk_matrix = doc_findings.get("contract_risk_matrix") or []

        expected_files = [c.get("contract", "") for c in contract_risk_matrix if c.get("contract")]

        return f"""You are running the test coverage evaluation step of a security pipeline.
Repository Location: {self.github_repo_path}

════════════════════════════════════════
EXPECTED CODELINE PRODUCTION TARGETS
════════════════════════════════════════
Protocol Name: {protocol_name or '(unknown)'}
Expected target files to review for coverage mapping: {json.dumps(expected_files)}

════════════════════════════════════════
INITIAL TEST DETECTION SIGNALS
════════════════════════════════════════
Found foundry.toml: {self.scan.has_foundry_toml}
Total test source files detected matching patterns: {self.scan.test_files_count}

--- [foundry.toml snippet] ---
{self.scan.foundry_toml_content}

--- [Detected test files layout (Max 30 listed)] ---
{self.scan.test_files_list}

════════════════════════════════════════
YOUR STRICTLY SCOPED TASK (IF TESTS EXIST)
════════════════════════════════════════
Your singular goal is to extract, calculate, and audit the code test coverage of this project.
CRITICAL MANDATE: Do NOT perform protocol research. Do NOT hunt for vulnerabilities, security flaws, or bugs. Focus purely on code execution metrics, compilation outputs, test paths, and test logs.

PHASE 1 — LOCATE AND INSPECT THE TEST SUITE PATHS
  1. Parse the `foundry.toml` provided above to identify configuration directory properties (like `test = '...'` or `src = '...'`). If not specified explicitly, assume the standard default directory structure (`test/` or `contracts/test/`).
  2. Confirm whether functional test assertions exist.

PHASE 2 — RUN GENERATIVE COVERAGE PROFILING
  1. Execute a structural analysis run using your available tooling via terminal interactions:
     `forge coverage --no-cache --out /tmp/out`
  2. If `forge coverage` fails due to compilation flags or library dependency configurations, execute manual standard test commands to deduce baseline behavior:
     `forge test -vvv --no-cache --out /tmp/out`

PHASE 3 — MANUALLY CROSS-CHECK FOR SILENT RUNNER CRASHES
  1. Review standard trace files or standard error logs to inspect that each target production contract was actually targeted.
  2. Cross-reference the final breakdown table lines directly against the "Expected target files" matrix listed above.
  3. Ensure that compilation warnings or truncated logs are caught and cataloged in the `suite_anomalies` list field.

════════════════════════════════════════
FINDINGS FORMAT SPECIFICATIONS
════════════════════════════════════════
If "has_tests" is false, set "global_metrics" to null, "per_file_coverage" to [], and summarize the lack of tests in the summary field.

Example structure for a project with verified test coverages:
{{
  "has_tests": true,
  "coverage_run_success": true,
  "global_metrics": {{
    "total_lines": 450,
    "covered_lines": 395,
    "coverage_percentage": 87.78
  }},
  "per_file_coverage": [
    {{
      "file_path": "src/Vault.sol",
      "covered_lines": 120,
      "total_lines": 120,
      "percentage": 100.0,
      "silent_failure_detected": false,
      "uncovered_regions": []
    }}
  ],
  "suite_anomalies": [],
  "summary": "Foundry test suite was successfully evaluated across the production codebases."
}}

════════════════════════════════════════
STRICT OPERATIONAL RULES
════════════════════════════════════════
• Filter out test files and helper libraries from `per_file_coverage`—only assess actual production logic files.
• Do not look for reentrancy, overflow bugs, or logic errors. If you find an uncovered line, log it as an uncovered range, nothing more.
"""
