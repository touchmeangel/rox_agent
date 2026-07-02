from collections.abc import Sequence

from ignite_core.debug_logger import default_debug_logger
from ignite_core.steps.base import BaseStep, StepResult
from ignite_core.tools import _download_store, _overflow_store
from rich.console import Console

console = Console()
COLOR = "#1dcccc"


class Workflow:
    def __init__(self, pipeline: Sequence[BaseStep]) -> None:
        self.pipeline = pipeline

    def run(
        self, repo_path: str, work_path: str, context: dict | None = None
    ) -> dict[str, StepResult]:
        if context is None:
            context = {}

        try:
            default_debug_logger.log_event(
                "agent_run_start",
                repo_path=repo_path,
                work_path=work_path,
            )

            results: dict[str, StepResult] = {}
            for step in self.pipeline:
                console.print(
                    f"\n[{COLOR}]●[/{COLOR}] [bold white]{step.name}[/bold white]"
                )

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
                    done = sum(
                        1 for item in result.plan if item.get("status") == "completed"
                    )
                    line += (
                        f"  [dim]·  {done}/{len(result.plan)} planned steps done[/dim]"
                    )
                console.print(line)
            _print_summary(results)
            default_debug_logger.log_event(
                "agent_run_complete",
                repo_path=repo_path,
                work_path=work_path,
                succeeded=sum(1 for r in results.values() if r.success),
                failed=sum(1 for r in results.values() if not r.success),
            )
            return results
        finally:
            _overflow_store.cleanup()
            _download_store.cleanup()


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
