from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ignite_agent.pipeline import PIPELINE
from ignite_agent.steps.base import StepResult
from ignite_agent.tools import default_debug_logger

console = Console()
color = "#1dcccc"

REPO_URL = "https://github.com/touchmeangel/ignite_agent"

ASCII_LOGO = r"""   __
__(o )>
\___)"""


def print_welcome_banner():
    from ignite_agent.config import MODELS

    logo = Text(ASCII_LOGO, style=color)

    header = Text.from_markup(
        "[bold white]ignite[/bold white][dim]  ·  EVM Smart Contract Security Research Agent[/dim]\n\n"
        "[white]Welcome back — point me at a repo and I'll start digging.[/white]\n"
    )

    models_table = Table.grid(padding=(0, 2))
    models_table.add_column()
    models_table.add_column(style="dim")
    models_table.add_column(style="dim")
    for key, cfg in MODELS.items():
        models_table.add_row(
            f"[{color}]{key}[/{color}]", cfg.model, f"({cfg.provider})"
        )

    footer = Text.from_markup(
        f"\n[dim underline link={REPO_URL}]{REPO_URL.removeprefix('https://')}[/]\n"
        f"[dim]{datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}[/dim]"
    )

    info = Group(header, models_table, footer)

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
    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Log the full, unsummarized stdout/stderr of every tool call to "
            "stderr — useful for seeing exactly what the model is seeing, "
            "and works fine in a container since it doesn't need a volume "
            "(just `docker logs`)."
        ),
    )
    parser.add_argument(
        "--debug-log",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "Write full tool I/O as JSONL to PATH instead of stderr. Implies --debug."
        ),
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

    _print_summary(results)

    if output_path:
        _write_results(results, output_path)

    return results


def _print_summary(results: dict[str, StepResult]) -> None:
    succeeded = sum(1 for r in results.values() if r.success)
    failed = len(results) - succeeded

    parts = []
    if succeeded:
        parts.append(f"[green]✓ {succeeded} succeeded[/green]")
    if failed:
        parts.append(f"[red]✗ {failed} failed[/red]")
    if not parts:
        parts.append("[dim]no steps ran[/dim]")

    console.print("\n" + "   ".join(parts) + "\n")


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

    debug_target = args.debug_log or ("stderr" if args.debug else None)
    default_debug_logger.configure(debug_target)

    print_welcome_banner()

    if debug_target:
        dest = (
            debug_target
            if debug_target != "stderr"
            else "stderr (this terminal / `docker logs`)"
        )
        console.print(f"[dim]Debug logging on → {dest}[/dim]\n")

    run_agent(project_path=args.project_path, output_path=args.output)


if __name__ == "__main__":
    main()
