"""
Audit orchestrator.

Runs steps in order. Each step receives the accumulated findings from all
prior steps as `context`.

Usage:
    python agent.py --project-path /my/contracts
    python agent.py --project-path /my/contracts --output results.json
"""

from __future__ import annotations

import json
import argparse
from datetime import datetime
from pathlib import Path

from config import PIPELINE
from steps.base import StepResult


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="EVM Smart Contract Audit Agent")

    parser.add_argument(
        "--project-path",
        type=str,
        required=False,
        default=".",
        help="Path to the smart contract project",
    )

    parser.add_argument(
        "--output",
        type=str,
        required=False,
        default=None,
        help="Optional path to save audit JSON results",
    )

    return parser.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

def run_audit(project_path: str, output_path: str | None = None) -> dict[str, StepResult]:
    print("\n" + "═" * 70)
    print("                🛡️  SOLIDITY AUDIT AGENT")
    print("═" * 70)
    print(f"📁 Project : {project_path}")
    print(f"⏱️  Started : {datetime.now().isoformat(timespec='seconds')}")
    print("═" * 70 + "\n")

    context: dict = {}
    results: dict[str, StepResult] = {}

    for StepClass in PIPELINE:
        step = StepClass()

        print(f"\n▶ Running step: {step.name}")
        print("-" * 70)

        result = step.run(context)
        results[step.name] = result

        context[step.name] = {
            "findings": result.findings,
            "summary": result.summary,
            "success": result.success,
        }

        status = "✓ completed" if result.success else "⚠ incomplete"
        print(f"\n[{step.name}] {status} in {result.iterations_used} iterations")
        print("-" * 70)

    # ── Write output only if requested ───────────────────────────────────────
    if output_path:
        _write_results(results, output_path)

    _print_summary(results, output_path)

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Output writer
# ─────────────────────────────────────────────────────────────────────────────

def _write_results(results: dict[str, StepResult], output_path: str) -> None:
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

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2))

    print("\n" + "═" * 70)
    print(f"💾 Audit results saved → {path}")
    print("═" * 70 + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# Pretty summary
# ─────────────────────────────────────────────────────────────────────────────

def _print_summary(results: dict[str, StepResult], output_path: str | None):
    total_steps = len(results)
    failed_steps = sum(1 for r in results.values() if not r.success)

    print("\n" + "═" * 70)
    print("                     📊 AUDIT SUMMARY")
    print("═" * 70)

    for name, r in results.items():
        status = "✓" if r.success else "⚠"
        print(f"{status} {name:<20} | iterations: {r.iterations_used:<3} | success: {r.success}")

    print("─" * 70)
    print(f"Steps total   : {total_steps}")
    print(f"Steps failed  : {failed_steps}")
    print(f"Output file   : {output_path if output_path else 'NOT SAVED'}")

    print("═" * 70 + "\n")

if __name__ == "__main__":
    args = parse_args()
    run_audit(
        project_path=args.project_path,
        output_path=args.output,
    )