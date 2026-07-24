from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ignite_coordinator.pipeline import MODELS, PIPELINE
from ignite_core.cli_common import IGNITE_COLOR, REPO_URL, add_common_run_args, aggregate_usage, console
from ignite_core.serialization import to_jsonable
from ignite_core.steps.base import StepResult
from ignite_core.workflow import Workflow

ASCII_LOGO = r"""   __
__(o )>
\___)"""


def add_coordinator_args(parser) -> None:
    add_common_run_args(parser)
    parser.add_argument(
        "--skip-build", action="store_true", help="Skip forge build verification.",
    )


def _print_welcome_banner() -> None:
    logo = Text(ASCII_LOGO, style=IGNITE_COLOR)
    header = Text.from_markup(
        "[bold white]ignite[/bold white][dim]  ·  EVM Smart Contract Security Research Agent[/dim]\n\n"
        "[white]Welcome back — point me at a repo and I'll start digging.[/white]\n"
    )

    models_table = Table.grid(padding=(0, 2))
    models_table.add_column(no_wrap=True)
    models_table.add_column(style="dim", no_wrap=True)
    models_table.add_column(style="dim", no_wrap=True)

    all_entries: list[tuple[str, Any]] = []
    for key, cfg_list in MODELS.items():
        if not cfg_list:
            continue
        for idx, model in enumerate(cfg_list):
            all_entries.append((key if idx == 0 else "", model))

    MAX_MODELS_SHOWN = 5
    shown = all_entries[:MAX_MODELS_SHOWN]
    remaining = len(all_entries) - len(shown)
    for label, model in shown:
        prefix = f"[{IGNITE_COLOR}]{label}[/{IGNITE_COLOR}]" if label else ""
        models_table.add_row(prefix, model.model, f"({model.provider})")
    if remaining > 0:
        plural = "s" if remaining != 1 else ""
        models_table.add_row("", f"[dim]+{remaining} more model{plural}[/dim]", "")

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
        "missions": [mission.to_dict() for mission in all_missions],
        "steps": {
            name: {
                "success": r.success,
                "iterations": r.iterations_used,
                "summary": r.summary,
                "findings": r.findings,
                "plan": r.plan,
                "blockers": r.blockers,
                "model_usage": r.model_usage,
            }
            for name, r in results.items()
        },
        "usage_summary": aggregate_usage(results),
    }

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(output), indent=2))


def run_coordinator(args) -> None:
    _print_welcome_banner()

    workflow = Workflow(PIPELINE)
    results: dict = {}
    try:
        results = workflow.run(args.repo_path, args.work_path)
    except Exception:
        results = workflow.results
        raise
    finally:
        if args.output:
            _write_results(results, args.output)