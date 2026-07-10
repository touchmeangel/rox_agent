from __future__ import annotations

import argparse
import json
from dataclasses import fields, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Union, get_args, get_origin

from ignite_core.debug_logger import default_debug_logger
from ignite_core.mission import Mission
from ignite_core.steps.base import StepResult
from ignite_core.workflow import Workflow
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
                "model_usage": r.model_usage,
            }
            for name, r in results.items()
        },
        "usage_summary": _aggregate_usage(results),
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2))


def _aggregate_usage(results: dict[str, StepResult]) -> dict:
    totals: dict[str, dict] = {}
    for r in results.values():
        for account_key, usage in (r.model_usage or {}).items():
            entry = totals.setdefault(
                account_key,
                {
                    "model": usage.get("model", ""),
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                },
            )
            entry["calls"] += usage.get("calls", 0)
            entry["input_tokens"] += usage.get("input_tokens", 0)
            entry["output_tokens"] += usage.get("output_tokens", 0)

    total_calls = sum(m["calls"] for m in totals.values())
    total_tokens = sum(m["input_tokens"] + m["output_tokens"] for m in totals.values())

    for m in totals.values():
        m["total_tokens"] = m["input_tokens"] + m["output_tokens"]
        m["pct_of_calls"] = (
            round(100 * m["calls"] / total_calls, 2) if total_calls else 0.0
        )
        m["pct_of_tokens"] = (
            round(100 * m["total_tokens"] / total_tokens, 2) if total_tokens else 0.0
        )

    return {
        "total_calls": total_calls,
        "total_tokens": total_tokens,
        "by_model": totals,
    }


def from_dict(cls, data):
    if data is None:
        return None

    if not is_dataclass(cls):
        return data

    kwargs = {}

    for field in fields(cls):
        value = data.get(field.name)
        typ = field.type

        origin = get_origin(typ)

        if is_dataclass(typ):
            kwargs[field.name] = from_dict(typ, value)

        elif origin is list:
            inner = get_args(typ)[0]
            if is_dataclass(inner):
                kwargs[field.name] = [from_dict(inner, v) for v in value]
            else:
                kwargs[field.name] = value

        elif origin is dict:
            kwargs[field.name] = value

        elif origin is Union:
            args = [a for a in get_args(typ) if a is not type(None)]
            if len(args) == 1 and is_dataclass(args[0]):
                kwargs[field.name] = (
                    None if value is None else from_dict(args[0], value)
                )
            else:
                kwargs[field.name] = value

        else:
            kwargs[field.name] = value

    return cls(**kwargs)


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

    parser.add_argument(
        "--missions-file",
        required=True,
        help="Path to coordinator results JSON containing coordinator.missions[].",
    )

    parser.add_argument(
        "--mission-id",
        required=True,
        help="ID of the mission within --missions-file this worker should run.",
    )

    return parser.parse_args()


def main():
    args = parse_args()
    default_debug_logger.configure(args.debug)
    print_worker_banner(args.repo_path)

    repo_path = args.repo_path
    work_path = args.work_path
    output_path = args.output
    data = json.loads(Path(args.missions_file).read_text())
    missions = data.get("missions", [])
    mission_data = next((m for m in missions if m["id"] == args.mission_id), None)
    if mission_data is None:
        raise SystemExit(
            f"mission id '{args.mission_id}' not found in {args.missions_file}"
        )

    mission = from_dict(Mission, mission_data)

    workflow = Workflow(PIPELINE)
    results: dict = {}

    try:
        results = workflow.run(
            repo_path,
            work_path,
            context={
                "mission": mission,
            },
        )
    except Exception:
        results = workflow.results
        raise
    finally:
        if output_path:
            _write_results(results, output_path)


if __name__ == "__main__":
    main()
