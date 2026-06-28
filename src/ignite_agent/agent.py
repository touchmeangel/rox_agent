from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path

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
    
    models_table.add_column(no_wrap=True)
    models_table.add_column(style="dim", no_wrap=True)
    models_table.add_column(style="dim", no_wrap=True)
    
    for key, cfg_list in MODELS.items():
        if not cfg_list:
            continue
            
        primary = cfg_list[0]
        models_table.add_row(
            f"[{color}]{key}[/{color}]", primary.model, f"({primary.provider})"
        )
        
        for fallback in cfg_list[1:]:
            models_table.add_row(
                "",
                f"[dim]⎿  {fallback.model}[/dim]", 
                f"[dim]({fallback.provider})[/dim]"
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
        "--repo-path",
        type=str,
        required=True,
        help="Path to the pre-cloned repository (read-only source of truth).",
    )
    parser.add_argument(
        "--work-path",
        type=str,
        required=True,
        help=(
            "Path to the writable workspace. The agent stages build artifacts "
            "and writes results here. Pass an existing workspace to skip "
            "rebuild phases once they are implemented."
        ),
    )
    parser.add_argument(
        "--output", type=str, required=False, default=None, help="JSON output path."
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


def _copy_repo_to_workspace(repo_path: str, work_path: str) -> None:
    shutil.copytree(repo_path, work_path, dirs_exist_ok=True)


def run_agent(
    repo_path: str,
    work_path: str,
    output_path: str | None = None,
) -> dict[str, StepResult]:
    default_debug_logger.log_event(
        "agent_run_start",
        repo_path=repo_path,
        work_path=work_path,
    )

    console.print(f"\n[{color}]●[/{color}] [bold white]Syncing workspace[/bold white]")
    try:
        _copy_repo_to_workspace(repo_path, work_path)
        console.print(
            f"  [dim]⎿[/dim]  [green]✓[/green]  [dim]{repo_path} → {work_path}[/dim]"
        )
    except Exception as e:
        console.print(f"  [dim]⎿[/dim]  [red]✗[/red]  [dim]{e}[/dim]")
        raise SystemExit(1) from None

    context: dict = {}
    results: dict[str, StepResult] = {}

    for step in PIPELINE:
        console.print(f"\n[{color}]●[/{color}] [bold white]{step.name}[/bold white]")

        step.github_repo_path = repo_path
        step.project_path = work_path

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
        repo_path=repo_path,
        work_path=work_path,
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


def main():
    args = parse_args()
    default_debug_logger.configure(args.debug)
    print_welcome_banner()

    run_agent(
        repo_path=args.repo_path,
        work_path=args.work_path,
        output_path=args.output,
    )


if __name__ == "__main__":
    main()
