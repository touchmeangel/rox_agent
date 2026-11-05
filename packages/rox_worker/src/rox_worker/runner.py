from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from rox_core.cli_common import (
    add_common_run_args,
    aggregate_usage,
    print_role_banner,
)
from rox_core.result import results_payload, publish_results
from rox_core.mission import Mission
from rox_core.serialization import from_dict
from rox_core.steps.base import StepResult
from rox_core.workflow import Workflow
from rox_worker.pipeline import PIPELINE


def add_worker_args(parser) -> None:
    add_common_run_args(parser)
    parser.add_argument(
        "--missions-file",
        required=True,
        help="Path to coordinator results JSON containing missions[].",
    )
    parser.add_argument(
        "--mission-id",
        required=True,
        help="ID of the mission within --missions-file this worker should run.",
    )


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
        "usage_summary": aggregate_usage(results),
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2))


def run_worker(args) -> None:
    print_role_banner("worker")

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
        results = workflow.run(args.work_path, context={"mission": mission})
    except Exception:
        results = workflow.results
        raise
    finally:
        uploaded = publish_results(results_payload(results), args.output)
    if not uploaded:
        raise SystemExit("result upload failed")
