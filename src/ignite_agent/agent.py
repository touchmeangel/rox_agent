from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
import tempfile
import subprocess

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ignite_agent.debug_logger import default_debug_logger
from ignite_agent.pipeline import PIPELINE
from ignite_agent.steps.base import StepResult

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
        "--github-url", type=str, required=True, help="GitHub repo URL to clone and audit"
    )
    parser.add_argument(
        "--project-path", type=str, required=False, default=".", help="Path to project"
    )
    parser.add_argument(
        "--output", type=str, required=False, default=None, help="JSON path output"
    )
    parser.add_argument(
        "--debug",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "Log full tool calls, full LLM requests/responses, and step "
            "lifecycle events. Pass 'stderr' (or '-') to print as JSONL to "
            "stderr — useful in a container since `docker logs` picks it up "
            "without a volume mount — or a file PATH to append JSONL there."
        ),
    )
    return parser.parse_args()


def run_agent(
    project_path: str, github_repo_path: str, output_path: str | None = None
) -> dict[str, StepResult]:
    default_debug_logger.log_event("agent_run_start", project_path=project_path)

    context: dict = {}
    results: dict[str, StepResult] = {}

    for step in PIPELINE:
        console.print(f"\n[{color}]●[/{color}] [bold white]{step.name}[/bold white]")

        step.project_path = project_path
        step.github_repo_path = github_repo_path
    
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
        line = (
            f"  [dim]⎿[/dim]  {icon}  "
            f"[dim]{result.iterations_used} iteration{plural}[/dim]"
        )
        if result.plan:
            done = sum(1 for item in result.plan if item.get("status") == "completed")
            line += f"  [dim]·  {done}/{len(result.plan)} planned steps done[/dim]"
        console.print(line)

    _print_summary(results)

    if output_path:
        _write_results(results, output_path)

    default_debug_logger.log_event(
        "agent_run_complete",
        project_path=project_path,
        succeeded=sum(1 for r in results.values() if r.success),
        failed=sum(1 for r in results.values() if not r.success),
    )

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
                "plan": r.plan,
                "blockers": r.blockers,
            }
            for name, r in results.items()
        },
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2))

def clone_repo(url: str, dest: str) -> None:
    result = subprocess.run(
        ["git", "clone", url, dest],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git clone failed:\n{result.stderr.strip()}")

def main():
    args = parse_args()
    default_debug_logger.configure(args.debug)
    print_welcome_banner()

    with tempfile.TemporaryDirectory(prefix="ignite_") as tmp:
        console.print(f"\n[{color}]●[/{color}] [bold white]Cloning repo[/bold white]")
        try:
            clone_repo(args.github_url, tmp)
            console.print(f"  [dim]⎿[/dim]  [green]✓[/green]  [dim]cloned to {tmp}[/dim]")
        except RuntimeError as e:
            console.print(f"  [dim]⎿[/dim]  [red]✗[/red]  [dim]{e}[/dim]")
            raise SystemExit(1)

        run_agent(project_path=args.project_path, github_repo_path=tmp, output_path=args.output,)

if __name__ == "__main__":
    main()
