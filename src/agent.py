"""
Audit orchestrator.

Runs steps in order. Each step receives the accumulated findings from all
prior steps as `context`, so later steps can reason about earlier ones.

Usage (from host via docker-compose):
    PROJECT_PATH=/my/contracts docker-compose up
"""

from __future__ import annotations
import json
import sys
from datetime import datetime
from pathlib import Path

from config import PROJECT_PATH, PIPELINE
from steps.base import StepResult


# ── Runner ────────────────────────────────────────────────────────────────────


def run_audit(project_path: str = PROJECT_PATH) -> dict[str, StepResult]:
    print(f"\n{'═' * 60}")
    print(f"  Solidity Audit Agent")
    print(f"  Project : {project_path}")
    print(f"  Started : {datetime.now().isoformat(timespec='seconds')}")
    print(f"{'═' * 60}")

    context: dict = {}  # findings passed step → step
    results: dict[str, StepResult] = {}

    for StepClass in PIPELINE:
        step = StepClass()
        result = step.run(context)
        results[step.name] = result

        # Pass this step's output to subsequent steps
        context[step.name] = {
            "findings": result.findings,
            "summary": result.summary,
            "success": result.success,
        }

        if not result.success:
            print(f"[{step.name}] ⚠  step did not complete successfully, continuing")

    # Write results to project directory
    _write_results(results, project_path)
    return results


def _write_results(results: dict[str, StepResult], project_path: str) -> None:
    output = {
        "generated_at": datetime.now().isoformat(),
        "steps": {
            name: {
                "success": r.success,
                "iterations": r.iterations_used,
                "summary": r.summary,
                "findings": r.findings,
            }
            for name, r in results.items()
        },
    }
    out_path = Path(project_path) / "audit_results.json"
    out_path.write_text(json.dumps(output, indent=2))
    print(f"\n{'═' * 60}")
    print(f"  Audit complete → {out_path}")
    print(f"{'═' * 60}\n")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else PROJECT_PATH
    run_audit(path)
