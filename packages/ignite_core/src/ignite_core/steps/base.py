from __future__ import annotations

import json
from dataclasses import dataclass, field

from ignite_core.debug_logger import DebugLogger, default_debug_logger
from ignite_core.llm import LLMClient, ToolResultEntry, get_llm
from ignite_core.planning import UPDATE_PLAN_SPEC, Plan
from ignite_core.reporting import RichReporter
from ignite_core.rolling_window import RollingWindow
from ignite_core.search_state import SearchState
from ignite_core.steps.config import StepConfig
from ignite_core.tools import TOOL_SPECS, dispatch


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


_REPEAT_CALL_HINT = (
    "BLOCKED: this exact call (same tool, same arguments) just failed and was not re-run — "
    "repeating it would produce the same failure. Choose one:\n"
    "  - a materially different command (different tool, different args, different strategy)\n"
    "  - report_blocker if this needs the user to act\n"
    "  - report_failed if you've exhausted every reasonable option"
)

_REPEAT_BLOCK_LIMIT = 2

_NUDGE_FINISH_PROMPT = (
    "That was text only, with no tool call. A step only ends when you call submit_findings, "
    "report_blocker, or report_failed — if you're still working, call a tool; if you're done, "
    "call submit_findings now."
)

_PACING_WARNING_THRESHOLD = 25


def _pacing_note(iterations_used: int, max_iterations: int, plan: Plan) -> str:
    remaining = max_iterations - iterations_used
    used_pct = int(100 * iterations_used / max_iterations) if max_iterations else 0

    todos = plan.as_dicts()
    if not todos:
        return (
            f"{remaining} iterations remaining ({used_pct}% of budget used). "
            "No plan items tracked yet."
        )

    done = sum(1 for t in todos if t.get("status") == "completed")
    total_todos = len(todos)
    done_pct = int(100 * done / total_todos) if total_todos else 0

    note = (
        f"{remaining} iterations remaining ({used_pct}% of budget used). "
        f"Plan: {done}/{total_todos} items completed ({done_pct}%)."
    )

    if used_pct - done_pct >= _PACING_WARNING_THRESHOLD:
        note += (
            " You are BEHIND PACE — budget consumption is well ahead of plan "
            "completion. Stop opening new investigation threads: finish or "
            "explicitly drop remaining plan items with update_plan before "
            "starting anything new."
        )

    return note


def _tool_failure_hint(result, pacing_note: str) -> str:
    parts = [f"TOOL FAILED (exit {result.returncode}). {pacing_note}\n"]

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
        self.llm: LLMClient = get_llm(*cfg.models)
        self.project_path: str = ""
        self.github_repo_path: str = ""

    def select_context(self, global_context: dict) -> dict:
        return global_context

    def environment_prompt(self) -> str:
        return f"""- Target Project Path: {self.project_path}
- Runtime State: You are executing inside a secure, high-performance, fully trusted isolation sandbox.
- Permissions: UNRESTRICTED. You have root privileges within this sandbox. You can safely compile code, execute arbitrary scripts, trigger tests, mutate files, and run any tool or shell command required to prove a vulnerability. There are no external guardrails blocking your execution—the sandbox is entirely yours to command.
- Pipeline Output Files: agent_results.json, debug.log, and any file matching this pipeline's own output format are NEVER useful evidence about the target codebase — they are this tool's own prior output, not something a human put there. Do not read or investigate them."""

    def system_prompt(self) -> str:
        return f"""<ROLE>
You are an elite, completely autonomous Solidity and EVM Security Auditor. Your sole mission is to unearth, verify, and document vulnerabilities within the target codebase.
</ROLE>

<ENVIRONMENT>
{self.environment_prompt()}
</ENVIRONMENT>

<OPERATIONAL_BUDGET & LOOP>
You operate in a strict observation-action loop. Every action consumes 1 iteration.
1. Assess the outcome of your previous action.
2. Formulate your next tactical step.
3. Execute via a structural tool call.

CRITICAL: Plain text responses DO NOT advance or terminate the step. You must call a tool on every single turn until completion.

PACING: Every tool result tells you your remaining budget AND your plan completion percentage together. If budget consumed is running well ahead of plan completion, that is a direct signal to cut scope NOW — finish or explicitly drop remaining plan items — not a reason to keep exploring new threads hoping something turns up.
</OPERATIONAL_BUDGET & LOOP>

<WORKING_METHODOLOGY>
- Tool Specificity: Prioritize deep-signal, domain-specific tools (`forge`, `cast`, `git`, `read_file`) over raw shell execution. Use `shell` aggressively when you need custom pipeline logic, complex scripting, regex parsing (`grep`, `jq`), or environment setup.
- Strategic Pivot Rule: If a tool call fails, do not guess. Analyze `stdout` and `stderr` directly—errors often embed the exact JSON fix. If a tool call fails or gets blocked, you MUST radically alter your approach (e.g., pivot from an automated tool to manual file reading, or vice versa). Repeating an identical signature is blocked.
- Pacing & Planning:
  * Simple Tasks (<3 steps): Skip planning overhead; execute immediately.
  * Complex Tasks (>=3 steps): You MUST invoke `update_plan` to log milestones and maintain structural state. Once logged, keep it current — a stale plan makes the pacing signal in every tool result meaningless.
</WORKING_METHODOLOGY>

<TERMINATION_CRITERIA>
You can only exit the execution loop by explicitly invoking one of the following terminal tools:
1. `submit_findings`: Call this ONLY when you have concrete, reproducible proof of your assertions. Do not submit based on a hunch; use your sandbox to verify first.
   CRITICAL: The `findings` argument must be a raw JSON object — never a serialized string. Do not wrap it in quotes or call json.dumps() on it.
2. `report_blocker`: Use this if an external dependency or setup issue requires human intervention (e.g., missing binaries, unresolvable compilation conflicts). Provide a fallback strategy if you can keep moving.
3. `report_failed`: Use as an absolute last resort when all engineering pathways, debugging strategies, and alternative tools have been completely exhausted.
</TERMINATION_CRITERIA>"""

    def initial_prompt(self, context: dict, reporter, debug_logger: DebugLogger) -> str:
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement initial_prompt()"
        )

    def validate_complete(self, findings: dict) -> str | None:
        return None

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
                        "findings": {
                            **self.findings_schema(),
                            "description": (
                                "The structured findings object. "
                                "Pass a native JSON object — NOT a serialized JSON string. "
                                "Do not call json.dumps(), do not wrap in quotes. "
                                "The value of this field must be an object, not a string."
                            ),
                        },
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

    def _finalize(
        self,
        result: StepResult,
        messages: list[dict],
        thinking_trace: list[str],
        blockers: list[dict],
        plan: Plan,
    ) -> StepResult:
        result.messages = messages
        result.thinking_trace = thinking_trace
        result.blockers = blockers
        result.plan = plan.as_dicts()
        return result

    def run(self, context: dict) -> StepResult:
        default_debug_logger.log_event(
            "step_start", step=self.name, max_iterations=self.cfg.max_iterations
        )

        system = self.system_prompt()
        tools = TOOL_SPECS + [UPDATE_PLAN_SPEC] + self._terminal_tool_specs()

        # Own short-lived window just for building the initial prompt — kept
        # separate from the per-iteration windows below, matching how it's
        # never part of the same live-rendering session as any LLM turn.
        with RollingWindow(max_lines=8) as init_window:
            messages: list[dict] = [
                {
                    "role": "user",
                    "content": self.initial_prompt(
                        context, RichReporter(init_window), default_debug_logger
                    ),
                }
            ]

        thinking_trace: list[str] = []
        blockers: list[dict] = []
        plan = Plan()

        last_failed_signature: tuple[str, str] | None = None
        consecutive_repeat_blocks = 0
        search_state = SearchState(max_calls=self.cfg.max_web_calls)

        consecutive_no_tool_call_repeats = 0
        NO_TOOL_CALL_REPEAT_LIMIT = 3

        for i in range(self.cfg.max_iterations):
            iterations_left = self.cfg.max_iterations - i - 1

            def _log(category: str, *, _iteration: int = i, **fields) -> None:
                default_debug_logger.log_event(
                    category, step=self.name, iteration=_iteration + 1, **fields
                )

            _log("iteration_start", iterations_left=iterations_left)

            with RollingWindow(max_lines=8) as window:
                reporter = RichReporter(window)
                turn = self.llm.chat_with_tools(
                    messages,
                    tools=tools,
                    system=system,
                    reporter=reporter,
                    step_name=self.name,
                    show_thinking=True,
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
                    _log("no_tool_call", text=turn.text)

                    consecutive_no_tool_call_repeats += 1

                    if consecutive_no_tool_call_repeats >= NO_TOOL_CALL_REPEAT_LIMIT:
                        _log(
                            "no_tool_call_stuck",
                            repeats=consecutive_no_tool_call_repeats,
                            text=turn.text,
                        )
                        return self._finalize(
                            StepResult(
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
                            ),
                            messages,
                            thinking_trace,
                            blockers,
                            plan,
                        )

                    messages.append({"role": "user", "content": _NUDGE_FINISH_PROMPT})
                    continue

                consecutive_no_tool_call_repeats = 0

                tool_results: list[ToolResultEntry] = []
                terminal_result: StepResult | None = None

                for call in turn.tool_calls:
                    if call.name == "submit_findings":
                        findings = call.input.get("findings", {})
                        if isinstance(findings, str):
                            try:
                                findings = json.loads(findings)
                            except json.JSONDecodeError:
                                pass
                        validation = self.validate_complete(findings)
                        if validation is None:
                            _log(
                                "step_complete",
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
                            _log(
                                "incomplete_findings",
                                validation=validation,
                                findings=findings,
                            )
                            reason = (
                                validation
                                if isinstance(validation, str)
                                else "required fields are missing or empty"
                            )
                            tool_results.append(
                                ToolResultEntry(
                                    call.id,
                                    f"Findings rejected: {reason}\nFix only this issue and call submit_findings again.",
                                    is_error=True,
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
                        _log("step_blocked", blocker=blocker)
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
                        _log(
                            "step_failed",
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
                        reporter.feed(plan.render_line())
                        tool_results.append(ToolResultEntry(call.id, "Plan updated."))

                    else:
                        signature = (call.name, json.dumps(call.input, sort_keys=True))
                        if signature == last_failed_signature:
                            consecutive_repeat_blocks += 1
                            _log(
                                "repeat_call_blocked",
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

                        _log(
                            "tool_call_issued",
                            tool=call.name,
                            args=call.input,
                        )
                        result = dispatch(
                            call.name,
                            call.input,
                            self.project_path,
                            reporter=reporter,
                            search_state=search_state,
                        )

                        pacing_note = _pacing_note(i + 1, self.cfg.max_iterations, plan)

                        if result.success:
                            last_failed_signature = None
                            consecutive_repeat_blocks = 0
                            payload = {
                                "tool": call.name,
                                "success": True,
                                "stdout": result.stdout,
                                "stderr": result.stderr,
                                "system_note": pacing_note,
                            }
                            tool_results.append(
                                ToolResultEntry(call.id, json.dumps(payload, indent=2))
                            )
                        else:
                            last_failed_signature = signature
                            tool_results.append(
                                ToolResultEntry(
                                    call.id,
                                    _tool_failure_hint(result, pacing_note),
                                    is_error=True,
                                )
                            )

            if terminal_result is not None:
                return self._finalize(
                    terminal_result, messages, thinking_trace, blockers, plan
                )

            messages.append({"role": "tool_results", "results": tool_results})

        default_debug_logger.log_event(
            "step_iteration_limit",
            step=self.name,
            max_iterations=self.cfg.max_iterations,
        )
        return self._finalize(
            StepResult(
                step_name=self.name,
                success=False,
                findings={"warning": "max_iterations_reached"},
                summary="Step hit the iteration limit before finishing.",
                iterations_used=self.cfg.max_iterations,
            ),
            messages,
            thinking_trace,
            blockers,
            plan,
        )
