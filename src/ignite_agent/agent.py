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

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ignite_agent.pipeline import PIPELINE
from ignite_agent.steps.base import StepResult

console = Console()
color = "#1dcccc"

REPO_URL = "https://github.com/touchmeangel/ignite_agent"

ASCII_LOGO = r"""   __
__(o )>
\___)"""


def print_welcome_banner():
    """Renders a clean, boxed startup interface."""
    logo = Text(ASCII_LOGO, style=color)

    info = Text.from_markup(
        "[bold white]ignite[/bold white][dim]  ·  EVM Smart Contract Security Research Agent[/dim]\n\n"
        "[white]Welcome back — point me at a repo and I'll start digging.[/white]\n\n"
        f"[dim underline link={REPO_URL}]{REPO_URL.removeprefix('https://')}[/]\n"
        f"[dim]{datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}[/dim]"
    )

    grid = Table.grid(padding=(0, 4))
    grid.add_column()
    grid.add_column()
    grid.add_row(logo, info)

    console.print()
    console.print(Panel(grid, box=box.ROUNDED, border_style="dim", padding=(1, 3)))
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

        console.print(f"\n[{color}]●[/{color}] [bold white]{step.name}[/bold white]")

        filtered_context = step.select_context(context)
        result = step.run(filtered_context)
        results[step.name] = result

        context[step.name] = {
            "findings": result.findings,
            "summary": result.summary,
            "success": result.success,
        }

        icon = "[green]✓[/green]" if result.success else "[red]✗[/red]"
        plural = "" if result.iterations_used == 1 else "s"
        console.print(
            f"  [dim]⎿[/dim]  {icon}  "
            f"[dim]{result.iterations_used} iteration{plural}[/dim]"
        )

    console.print()

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
