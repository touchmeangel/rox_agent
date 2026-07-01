from __future__ import annotations

import json
import argparse
from datetime import datetime
from pathlib import Path

from ignite_core.steps.base import StepResult
from ignite_core.workflow import Workflow
from ignite_core.debug_logger import default_debug_logger
from ignite_worker.pipeline import PIPELINE

from rich.console import Console

console = Console()
color = "#1dcccc"

REPO_URL = "https://github.com/touchmeangel/ignite_agent"

def print_worker_banner(repo_path: str) -> None:
    console.print()
    console.print(
        f"[{color}]ignite[/] [dim]worker[/dim]  "
        f"[dim]•[/dim]  "
        f"[white]{Path(repo_path).name}[/white]"
    )
    console.print(f"[dim]{REPO_URL}[/dim]")
    console.print()

def _write_results(results: dict[str, StepResult], output_path: str) -> None:
    output = {
        "generated_at": datetime.now().isoformat(),
        "steps": {
            name: {
                "success": r.success,
                "iterations": r.iterations_used,
                "summary": r.summary,
                "findings": r.findings,
                "plan": r.plan,
                "blockers": r.blockers,
            }
            for name, r in results.items()
        },
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="EVM Smart Contract Audit Agent | ignite"
    )

    parser.add_argument(
        "--repo-path",
        required=True,
        help="Path to the pre-cloned repository (read-only source of truth).",
    )

    parser.add_argument(
        "--work-path",
        required=True,
        help=(
            "Path to the writable workspace. The agent stages build artifacts "
            "and writes results here."
        ),
    )

    parser.add_argument(
        "--output",
        default=None,
        help="JSON output path.",
    )

    parser.add_argument(
        "--debug",
        default=None,
        metavar="PATH",
        help=(
            "Log full tool calls, LLM requests/responses, and lifecycle events. "
            "Pass 'stderr' (or '-') or a file path."
        ),
    )

    return parser.parse_args()

def main():
    args = parse_args()
    default_debug_logger.configure(args.debug)
    print_worker_banner(args.repo_path)

    repo_path = args.repo_path
    work_path = args.work_path
    output_path = args.output
        
    workflow = Workflow(PIPELINE)
    results = workflow.run(repo_path, work_path)
        
    if output_path:
        _write_results(results, output_path)

if __name__ == "__main__":
    main()
