"""
Audit orchestrator.

Runs steps in order. Each step receives the accumulated findings from all
prior steps as `context`.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.text import Text

from ignite_agent.pipeline import PIPELINE
from ignite_agent.steps.base import StepResult

console = Console()

ASCII_LOGO = """
hiii
"""


def print_welcome_banner():
    """Renders a clean, aesthetic startup interface."""
    logo_text = Text(ASCII_LOGO, style="dim white")

    meta_text = Text.assemble(
        (
            "\n──────────────────────────────────────────────────────────────\n",
            "dim #444444",
        ),
        (" ENGINE ", "bold bg:#333333 #ffffff"),
        ("  ignite EVM Autonomous Agent\n", "bold #b8bb26"),
        (" SOURCE ", "bold bg:#333333 #ffffff"),
        ("  https://github.com/touchmeangel/ignite_agent\n", "italic #83a598"),
        (" TIME   ", "bold bg:#333333 #ffffff"),
        f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
        (
            "──────────────────────────────────────────────────────────────",
            "dim #444444",
        ),
    )

    console.print(logo_text)
    console.print(meta_text)
    console.print()


def parse_args():
    parser = argparse.ArgumentParser(
        description="EVM Smart Contract Audit Agent | ignite"
    )
    parser.add_argument(
        "--project-path", type=str, required=False, default=".", help="Path to project"
    )
    parser.add_argument(
        "--output", type=str, required=False, default=None, help="JSON path output"
    )
    return parser.parse_args()


def run_agent(
    project_path: str, output_path: str | None = None
) -> dict[str, StepResult]:
    context: dict = {}
    results: dict[str, StepResult] = {}

    for StepClass in PIPELINE:
        step = StepClass()

        console.print(
            f"\n[bold #fe8019]▶[/bold #fe8019] [bold white]Running node processing phase:[/bold white] [cyan]{step.name}[/cyan]"
        )
        console.print(
            "[dim]#─────────────────────────────────────────────────────────────────────[/dim]"
        )

        filtered_context = step.select_context(context)
        result = step.run(filtered_context)
        results[step.name] = result

        context[step.name] = {
            "findings": result.findings,
            "summary": result.summary,
            "success": result.success,
        }

        status = (
            "[green]✓ completed[/green]" if result.success else "[red]† failed[/red]"
        )
        console.print(
            f"\n[dim]#[/dim] Phase {status} using [yellow]{result.iterations_used}[/yellow] reasoning iterations."
        )
        console.print(
            "[dim]#─────────────────────────────────────────────────────────────────────[/dim]\n"
        )

    if output_path:
        _write_results(results, output_path)

    return results


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


def main():
    args = parse_args()
    print_welcome_banner()
    run_agent(project_path=args.project_path, output_path=args.output)


if __name__ == "__main__":
    main()
