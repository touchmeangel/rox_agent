import traceback
from collections.abc import Sequence

from ignite_core.debug_logger import default_debug_logger
from ignite_core.reporting import Reporter, RichReporter
from ignite_core.rolling_window import RollingWindow
from ignite_core.steps.base import BaseStep, StepResult
from ignite_core.steps.mixins import GitHistoryAccessMixin
from ignite_core.tools import _download_store, _overflow_store


class Workflow:
    def __init__(
        self, pipeline: Sequence[BaseStep], reporter: Reporter | None = None
    ) -> None:
        self.pipeline = pipeline
        self._owns_reporter = reporter is None
        self._window: RollingWindow | None = None
        if reporter is None:
            self._window = RollingWindow()
            self._window.__enter__()
            reporter = RichReporter(self._window)
        self.reporter = reporter
        self.results: dict[str, StepResult] = {}

    def run(
        self, repo_path: str, work_path: str, context: dict | None = None
    ) -> dict[str, StepResult]:
        if context is None:
            context = {}
        self.results = {}

        try:
            default_debug_logger.log_event(
                "agent_run_start",
                repo_path=repo_path,
                work_path=work_path,
            )

            for step in self.pipeline:
                if isinstance(step, GitHistoryAccessMixin):
                    step.github_repo_path = repo_path
                step.project_path = work_path

                self.reporter.step_started(step.name, budget=step._iteration_budget())

                filtered_context = step.select_context(context)

                try:
                    result = step.run(filtered_context)
                except Exception as exc:
                    tb = traceback.format_exc()
                    default_debug_logger.log_event(
                        "step_crashed",
                        step=step.name,
                        error_type=type(exc).__name__,
                        error=str(exc),
                        traceback=tb,
                    )
                    result = StepResult(
                        step_name=step.name,
                        success=False,
                        findings={
                            "error": {
                                "type": type(exc).__name__,
                                "message": str(exc),
                                "traceback": tb,
                            }
                        },
                        summary=f"[CRASHED] {type(exc).__name__}: {exc}",
                        iterations_used=0,
                    )
                    self.results[step.name] = result
                    context[step.name] = {
                        "findings": result.findings,
                        "summary": result.summary,
                        "success": result.success,
                    }
                    self.reporter.step_finished(step.name, False, 0, None, None)
                    raise

                self.results[step.name] = result

                context[step.name] = {
                    "findings": result.findings,
                    "summary": result.summary,
                    "success": result.success,
                }

                plan_done = plan_total = None
                if result.plan:
                    plan_total = len(result.plan)
                    plan_done = sum(
                        1 for item in result.plan if item.get("status") == "completed"
                    )
                self.reporter.step_finished(
                    step.name,
                    result.success,
                    result.iterations_used,
                    plan_done,
                    plan_total,
                )

            succeeded = sum(1 for r in self.results.values() if r.success)
            failed = len(self.results) - succeeded
            self.reporter.run_summary(succeeded, failed)

            default_debug_logger.log_event(
                "agent_run_complete",
                repo_path=repo_path,
                work_path=work_path,
                succeeded=succeeded,
                failed=failed,
            )
            return self.results
        finally:
            _overflow_store.cleanup()
            _download_store.cleanup()
            if self._owns_reporter and self._window is not None:
                self._window.__exit__(None, None, None)
