from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from ignite_coordinator.pipeline import MODELS, PIPELINE
from ignite_core.debug_logger import default_debug_logger
from ignite_core.steps.base import StepResult
from ignite_core.workflow import Workflow
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()
color = "#1dcccc"

REPO_URL = "https://github.com/touchmeangel/ignite_agent"

ASCII_LOGO = r"""   __
__(o )>
\___)"""


def print_welcome_banner():
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

        for model in cfg_list[1:]:
            models_table.add_row(
                "",
                f"[dim]{model.model}[/dim]",
                f"[dim]({model.provider})[/dim]",
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


def to_jsonable(obj):
    if is_dataclass(obj):
        return to_jsonable(asdict(obj))

    if isinstance(obj, Enum):
        return obj.value

    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}

    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]

    return obj


def _missions_from_findings(findings: dict) -> list[Any]:
    if not isinstance(findings, dict):
        return []
    return findings.get("missions", []) or []


def _write_results(results: dict[str, StepResult], output_path: str) -> None:
    all_missions: list[Any] = []
    for r in results.values():
        all_missions.extend(_missions_from_findings(r.findings))

    output = {
        "generated_at": datetime.now().isoformat(),
        "missions": to_jsonable(all_missions),
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
    path.write_text(json.dumps(to_jsonable(output), indent=2))


def main():
    args = parse_args()
    default_debug_logger.configure(args.debug)
    print_welcome_banner()

    repo_path = args.repo_path
    work_path = args.work_path
    output_path = args.output

    workflow = Workflow(PIPELINE)
    results: dict = {}
    
    try:
        results = workflow.run(repo_path, work_path)
    except Exception:
        results = workflow.results
        raise
    finally:
        if output_path:
            _write_results(results, output_path)


if __name__ == "__main__":
    main()
