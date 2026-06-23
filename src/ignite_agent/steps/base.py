from __future__ import annotations

import json
from dataclasses import dataclass, field

from ignite_agent.config import PROJECT_PATH
from ignite_agent.debug_logger import default_debug_logger
from ignite_agent.llm import LLMClient, ToolResultEntry, get_llm
from ignite_agent.planning import UPDATE_PLAN_SPEC, Plan
from ignite_agent.rolling_window import RollingWindow
from ignite_agent.search_state import SearchState
from ignite_agent.steps.config import StepConfig
from ignite_agent.tools import TOOL_SPECS, dispatch


@dataclass
class StepResult:
    step_name: str
    success: bool
    findings: dict
    summary: str
    iterations_used: int
    messages: list[dict] = field(default_factory=list)
    thinking_trace: list[str] = field(default_factory=list)
    blockers: list[dict] = field(default_factory=list)
    plan: list[dict] = field(default_factory=list)


_INCOMPLETE_FINDINGS_HINT = (
    "Those findings are incomplete — required fields are missing or empty. "
    "Review what you have, fill in the gaps, and call submit_findings again when it's complete."
)

_REPEAT_CALL_HINT = (
    "BLOCKED: this exact call (same tool, same arguments) just failed and was not re-run — "
    "repeating it would produce the same failure. Choose one:\n"
    "  - a materially different command (different tool, different args, different strategy)\n"
    "  - report_blocker if this needs the user to act\n"
    "  - report_failed if you've exhausted every reasonable option"
)

_REPEAT_BLOCK_LIMIT = 2  # consecutive blocks of the *same* call before giving up

_NUDGE_FINISH_PROMPT = (
    "That was text only, with no tool call. A step only ends when you call submit_findings, "
    "report_blocker, or report_failed — if you're still working, call a tool; if you're done, "
    "call submit_findings now."
)


def _tool_failure_hint(result, iterations_left: int) -> str:
    parts = [
        f"TOOL FAILED (exit {result.returncode}). {iterations_left} iterations remain.\n"
    ]

    if result.stdout:
        parts.append(f"stdout:\n{result.stdout}\n")

    if result.stderr:
        parts.append(f"stderr:\n{result.stderr}\n")

    parts.append(
        "Diagnose before retrying — read the output above. "
        "web_search the exact error text if you don't recognise this failure. "
        "This exact call will be blocked if you repeat it unmodified."
    )
    return "\n".join(parts)


class BaseStep:
    name: str = "base"

    def __init__(self, cfg: StepConfig):
        self.cfg = cfg
        self.llm: LLMClient = get_llm(cfg.model_key)
        self.project_path: str = PROJECT_PATH

    def select_context(self, global_context: dict) -> dict:
        return global_context

    def system_prompt(self) -> str:
        return f"""You are an expert Solidity / EVM security auditor running inside an automated pipeline.

<context>
The project under audit is at: {self.project_path}
</context>

<working_method>
You work in a loop: read the last tool result, decide what it tells you, then act — either by
calling a tool, or, once you're confident, by finishing the step (see <finishing>).

A few habits that matter:
  - When a tool call fails, diagnose before retrying. Read the actual error — especially
    check stdout, which may contain useful JSON output even on non-zero exit codes.
    Never repeat the exact same tool call with the exact same arguments twice in a row —
    the harness will block it. Change something concrete instead.
  - You don't need a rigid checklist before every tool call — a short, concrete reason for
    the action you're about to take is enough. Spend more words reasoning when it's actually
    warranted: conflicting signals, an unfamiliar failure, or choosing between two real strategies.
  - Use update_plan for any step with three or more distinct pieces of work, so progress stays
    visible. Skip it for anything you can finish in one or two tool calls.
  - Prefer the most specific tool that fits (slither/forge/cast/git/read_file) over shell — it
    gives cleaner signal about what you're doing. Reach for shell when you genuinely need
    general-purpose composition (grep, jq, etc).
</working_method>

<finishing>
A step only ends when you call one of these three tools — plain text alone does not end it:
  - submit_findings — you're done and confident in the result.
  - report_blocker — a genuine environment problem (missing tool, broken install, missing
    credentials) that you can't resolve yourself. Set fallback_action + can_continue if you
    have a reasonable workaround; otherwise leave can_continue false.
  - report_failed — you've exhausted every reasonable option and the task cannot complete.
Don't call submit_findings until you've actually verified what you're reporting.
</finishing>"""

    def initial_prompt(self, context: dict) -> str:
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement initial_prompt()"
        )

    def validate_complete(self, findings: dict) -> bool:
        return True

    def findings_schema(self) -> dict:
        return {"type": "object"}

    def _terminal_tool_specs(self) -> list[dict]:
        return [
            {
                "name": "submit_findings",
                "description": (
                    "Finish this step successfully. Only call this once you've actually "
                    "verified what you're reporting — not on a hunch."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "findings": self.findings_schema(),
                        "summary": {
                            "type": "string",
                            "description": "Concise paragraph summarizing what you found.",
                        },
                    },
                    "required": ["findings", "summary"],
                },
            },
            {
                "name": "report_blocker",
                "description": (
                    "Report a genuine environment issue that needs the user to act before "
                    "this step can finish (missing tool, broken install, missing credentials, "
                    "permissions, etc) — not a finding about the audited code itself."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "issue": {
                            "type": "string",
                            "description": "What is blocking, specifically.",
                        },
                        "suggested_fix": {
                            "type": "string",
                            "description": "Exact command or step the user should run.",
                        },
                        "fallback_action": {
                            "type": ["string", "null"],
                            "description": "What you'll try instead, or null if there isn't one.",
                        },
                        "can_continue": {
                            "type": "boolean",
                            "description": "True if you can proceed with fallback_action right now without waiting for the user.",
                        },
                    },
                    "required": ["issue", "suggested_fix", "can_continue"],
                },
            },
            {
                "name": "report_failed",
                "description": "Report that this step is unrecoverable after exhausting every reasonable option.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "error": {"type": "string", "description": "Technical error."},
                        "reason": {
                            "type": "string",
                            "description": "Why this cannot be recovered automatically.",
                        },
                    },
                    "required": ["error", "reason"],
                },
            },
        ]

    def run(self, context: dict) -> StepResult:
        if not self.cfg.enabled:
            return StepResult(self.name, True, {}, "Step disabled.", 0)

        default_debug_logger.log_event(
            "step_start", step=self.name, max_iterations=self.cfg.max_iterations
        )

        system = self.system_prompt()
        tools = TOOL_SPECS + [UPDATE_PLAN_SPEC] + self._terminal_tool_specs()

        messages: list[dict] = [
            {"role": "user", "content": self.initial_prompt(context)}
        ]
        thinking_trace: list[str] = []
        blockers: list[dict] = []
        plan = Plan()

        last_failed_signature: tuple[str, str] | None = None
        consecutive_repeat_blocks = 0
        search_state = SearchState()

        consecutive_no_tool_call_repeats = 0
        NO_TOOL_CALL_REPEAT_LIMIT = 3

        for i in range(self.cfg.max_iterations):
            iterations_left = self.cfg.max_iterations - i - 1

            default_debug_logger.log_event(
                "iteration_start",
                step=self.name,
                iteration=i + 1,
                iterations_left=iterations_left,
            )

            with RollingWindow(max_lines=8) as window:
                turn = self.llm.chat_with_tools(
                    messages,
                    tools=tools,
                    system=system,
                    window=window,
                    step_name=self.name,
                )
                messages.append(
                    {
                        "role": "assistant",
                        "text": turn.text,
                        "tool_calls": turn.tool_calls,
                        "raw": turn.raw,
                    }
                )

                rationale = turn.thinking or turn.text
                if rationale:
                    thinking_trace.append(rationale)

                if not turn.tool_calls:
                    default_debug_logger.log_event(
                        "no_tool_call", step=self.name, iteration=i + 1, text=turn.text
                    )

                    consecutive_no_tool_call_repeats += 1

                    if consecutive_no_tool_call_repeats >= NO_TOOL_CALL_REPEAT_LIMIT:
                        default_debug_logger.log_event(
                            "no_tool_call_stuck",
                            step=self.name,
                            iteration=i + 1,
                            repeats=consecutive_no_tool_call_repeats,
                            text=turn.text,
                        )
                        return StepResult(
                            step_name=self.name,
                            success=False,
                            findings={
                                "error": {
                                    "message": (
                                        "Model repeated the same text-only response "
                                        f"{consecutive_no_tool_call_repeats} times without ever "
                                        "producing a real tool call."
                                    ),
                                    "reason": (
                                        "No native tool_calls were ever populated for this "
                                        "provider/model — this usually means the model or provider "
                                        "isn't emitting structured tool calls. Check the model's "
                                        "tool-calling support."
                                    ),
                                }
                            },
                            summary=(
                                "Step aborted early — the model never produced a real tool call, "
                                "it only wrote text that looked like one."
                            ),
                            iterations_used=i + 1,
                            messages=messages,
                            thinking_trace=thinking_trace,
                            blockers=blockers,
                            plan=plan.as_dicts(),
                        )

                    messages.append({"role": "user", "content": _NUDGE_FINISH_PROMPT})
                    continue

                consecutive_no_tool_call_repeats = 0

                tool_results: list[ToolResultEntry] = []
                terminal_result: StepResult | None = None

                for call in turn.tool_calls:
                    if call.name == "submit_findings":
                        findings = call.input.get("findings", {})
                        if self.validate_complete(findings):
                            default_debug_logger.log_event(
                                "step_complete",
                                step=self.name,
                                iteration=i + 1,
                                summary=call.input.get("summary", ""),
                                findings=findings,
                            )
                            terminal_result = StepResult(
                                step_name=self.name,
                                success=True,
                                findings=findings,
                                summary=call.input.get("summary", ""),
                                iterations_used=i + 1,
                            )
                            tool_results.append(
                                ToolResultEntry(call.id, "Findings accepted.")
                            )
                        else:
                            default_debug_logger.log_event(
                                "incomplete_findings",
                                step=self.name,
                                iteration=i + 1,
                                findings=findings,
                            )
                            tool_results.append(
                                ToolResultEntry(
                                    call.id, _INCOMPLETE_FINDINGS_HINT, is_error=True
                                )
                            )

                    elif call.name == "report_blocker":
                        blocker = {
                            "issue": call.input.get("issue", ""),
                            "suggested_fix": call.input.get("suggested_fix", ""),
                            "fallback_action": call.input.get("fallback_action"),
                            "can_continue": bool(call.input.get("can_continue", False)),
                        }
                        blockers.append(blocker)
                        default_debug_logger.log_event(
                            "step_blocked",
                            step=self.name,
                            iteration=i + 1,
                            blocker=blocker,
                        )
                        if blocker["fallback_action"] and blocker["can_continue"]:
                            tool_results.append(
                                ToolResultEntry(
                                    call.id,
                                    "Logged; the user will be notified. Proceed with your "
                                    f"stated fallback now: {blocker['fallback_action']}",
                                )
                            )
                            last_failed_signature = None
                            consecutive_repeat_blocks = 0
                        else:
                            terminal_result = StepResult(
                                step_name=self.name,
                                success=False,
                                findings={"blocked": blocker},
                                summary=f"[BLOCKED] {blocker['issue']}. Fix: {blocker['suggested_fix']}",
                                iterations_used=i + 1,
                            )
                            tool_results.append(
                                ToolResultEntry(call.id, "Blocker recorded.")
                            )

                    elif call.name == "report_failed":
                        default_debug_logger.log_event(
                            "step_failed",
                            step=self.name,
                            iteration=i + 1,
                            error=call.input.get("error"),
                            reason=call.input.get("reason"),
                        )
                        terminal_result = StepResult(
                            step_name=self.name,
                            success=False,
                            findings={
                                "error": {
                                    "message": call.input.get("error", "unknown error"),
                                    "reason": call.input.get(
                                        "reason", "unknown failure"
                                    ),
                                }
                            },
                            summary=f"[FAILED] {call.input.get('reason', '')}",
                            iterations_used=i + 1,
                        )
                        tool_results.append(
                            ToolResultEntry(call.id, "Failure recorded.")
                        )

                    elif call.name == "update_plan":
                        plan.update(call.input.get("todos", []))
                        window.feed(plan.render_line())
                        tool_results.append(ToolResultEntry(call.id, "Plan updated."))

                    else:
                        signature = (call.name, json.dumps(call.input, sort_keys=True))
                        if signature == last_failed_signature:
                            consecutive_repeat_blocks += 1
                            default_debug_logger.log_event(
                                "repeat_call_blocked",
                                step=self.name,
                                iteration=i + 1,
                                tool=call.name,
                                args=call.input,
                                repeats=consecutive_repeat_blocks,
                            )
                            tool_results.append(
                                ToolResultEntry(
                                    call.id, _REPEAT_CALL_HINT, is_error=True
                                )
                            )

                            if consecutive_repeat_blocks >= _REPEAT_BLOCK_LIMIT:
                                terminal_result = StepResult(
                                    step_name=self.name,
                                    success=False,
                                    findings={
                                        "error": {
                                            "message": (
                                                f"Model repeated the same blocked call ({call.name}) "
                                                f"{consecutive_repeat_blocks} times in a row."
                                            ),
                                            "reason": (
                                                "After the first real failure, every retry was the exact "
                                                "same call with the exact same arguments — each one was "
                                                "blocked client-side without re-executing."
                                            ),
                                        }
                                    },
                                    summary=(
                                        f"Step aborted early — stuck repeating a blocked '{call.name}' call."
                                    ),
                                    iterations_used=i + 1,
                                )
                            continue

                        default_debug_logger.log_event(
                            "tool_call_issued",
                            step=self.name,
                            iteration=i + 1,
                            tool=call.name,
                            args=call.input,
                        )
                        result = dispatch(
                            call.name,
                            call.input,
                            self.project_path,
                            window=window,
                            search_state=search_state,
                        )

                        if result.success:
                            last_failed_signature = None
                            consecutive_repeat_blocks = 0
                            payload = {
                                "tool": call.name,
                                "success": True,
                                "stdout": result.stdout,
                                "stderr": result.stderr,
                                "system_note": f"{iterations_left} iterations remaining.",
                            }
                            tool_results.append(
                                ToolResultEntry(call.id, json.dumps(payload, indent=2))
                            )
                        else:
                            last_failed_signature = signature
                            tool_results.append(
                                ToolResultEntry(
                                    call.id,
                                    _tool_failure_hint(result, iterations_left),
                                    is_error=True,
                                )
                            )

                if terminal_result is not None:
                    terminal_result.messages = messages
                    terminal_result.thinking_trace = thinking_trace
                    terminal_result.blockers = blockers
                    terminal_result.plan = plan.as_dicts()
                    return terminal_result

                messages.append({"role": "tool_results", "results": tool_results})

        default_debug_logger.log_event(
            "step_iteration_limit",
            step=self.name,
            max_iterations=self.cfg.max_iterations,
        )
        return StepResult(
            step_name=self.name,
            success=False,
            findings={"warning": "max_iterations_reached"},
            summary="Step hit the iteration limit before finishing.",
            iterations_used=self.cfg.max_iterations,
            messages=messages,
            thinking_trace=thinking_trace,
            blockers=blockers,
            plan=plan.as_dicts(),
        )
