from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ignite_agent.config import PROJECT_PATH, STEPS, StepConfig
from ignite_agent.debug_logger import default_debug_logger
from ignite_agent.llm import LLMClient, RollingWindow, get_llm
from ignite_agent.search_state import SearchState
from ignite_agent.tools import TOOL_DESCRIPTIONS, dispatch


@dataclass
class StepResult:
    step_name: str
    success: bool
    findings: dict
    summary: str
    iterations_used: int
    messages: list[dict] = field(default_factory=list)
    thinking_trace: list[str] = field(default_factory=list)  # captured model reasoning
    blockers: list[dict] = field(default_factory=list)  # user-side issues surfaced


_INVALID_JSON_PROMPT = (
    "Your response was not valid JSON. "
    'Reply with a single JSON object that always includes a "thinking" field.'
)

_UNKNOWN_STATUS_PROMPT = (
    "Unknown status '{status}'. "
    "Valid values: 'tool_call', 'complete', 'blocked', 'failed'."
)

_INCOMPLETE_FINDINGS_PROMPT = (
    "The findings are incomplete — required fields are missing. "
    "Review what you have, fill in the gaps, and reply with a complete 'complete' object."
)

_FAILURE_RETRY_PROMPT = """\
TOOL FAILED (exit {returncode}). {iterations_left} iterations remain.

stderr:
{stderr}

Apply OBSERVE → ANALYZE → PLAN → ACT (see your instructions). If this error is \
unfamiliar, your PLAN should include a web_search — if you're unsure how to phrase \
it, this query is extracted directly from the error and is a safe starting point:

  "{suggested_query}"

This exact command may not be repeated unmodified — a repeat will be blocked \
before it runs.\
"""

_REPEAT_CALL_PROMPT = """\
BLOCKED: {tool} {args} just failed and was not re-run — repeating it would \
produce the same failure. {iterations_left} iterations remain.

Choose one:
  • a materially different command (different tool, different args, different strategy)
  • status "blocked" if this needs the user to act
  • status "failed" if you've exhausted every reasonable option
"""


def _extract_json(text: str) -> dict | None:
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    m = re.search(r"```(?:json)?\s*(\{.*?})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    m = re.search(r"\{.*}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    return None


_ERROR_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)):?\s*(.*)$")


def _suggest_search_query(stderr: str, max_len: int = 120) -> str | None:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return None

    # Prefer the last line that looks like "SomeError: message" / "SomeException: message"
    for line in reversed(lines):
        m = _ERROR_LINE_RE.match(line)
        if m:
            exc_name, msg = m.groups()
            return f"{exc_name} {msg}".strip()[:max_len]

    return lines[-1][:max_len]


class BaseStep:
    name: str = "base"

    def __init__(self):
        cfg: StepConfig = STEPS[self.name]
        self.cfg = cfg
        self.llm: LLMClient = get_llm(cfg.model_key)
        self.project_path: str = PROJECT_PATH

    def select_context(self, global_context: dict) -> dict:
        return global_context

    def system_prompt(self) -> str:
        tools_block = "\n".join(
            f"  {name}: {desc}" for name, desc in TOOL_DESCRIPTIONS.items()
        )
        return f"""You are an expert Solidity / EVM security auditor running inside an automated pipeline.

The project under audit is at: {self.project_path}

Available tools:
{tools_block}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OBSERVE → THINK → ACT  (mandatory for every turn)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Every response MUST include a "thinking" field. Use it to reason before acting.

Structure your thinking as:
  OBSERVE:  What did the last tool output tell me?
  ANALYZE:  What does this mean? Real issue or noise? Conflicting signals?
  PLAN:     What are my options here, and which is best?
  ACT:      What am I about to do, exactly?

When a tool fails:
  • Don't give up immediately — think about root cause first.
  • If the error is unfamiliar, web_search the exact error string or exception
    name (not a vague description), and web_fetch a result if the snippet
    alone isn't enough to act on.
  • Never repeat the exact same tool call twice in a row — if it failed once,
    the next attempt must be a genuinely different approach.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RESPONSE SHAPES  (exactly one JSON object, no prose outside it)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Run a tool:
{{
  "thinking":  "<OBSERVE / ANALYZE / PLAN / ACT>",
  "status":    "tool_call",
  "tool":      "<tool_name>",
  "args":      "<arguments string>",
  "reason":    "<one sentence: why this specific call>"
}}

Step complete:
{{
  "thinking":  "<what I found; am I confident; any remaining gaps>",
  "status":    "complete",
  "findings":  {{ ... }},
  "summary":   "<concise paragraph>"
}}

Blocked — a genuine environment issue the user must fix first:
{{
  "thinking":        "<root-cause reasoning; what I already tried>",
  "status":          "blocked",
  "issue":           "<what is blocking, specifically>",
  "suggested_fix":   "<exact command or step the user should run>",
  "fallback_action": "<what I will try instead — or null if none>",
  "can_continue":    true | false
}}

Failed — unrecoverable after exhausting all options:
{{
  "thinking": "<everything I tried and why nothing worked>",
  "status":   "failed",
  "error":    "<technical error>",
  "reason":   "<why this cannot be recovered automatically>"
}}

Invalid JSON or unknown status → a retry prompt will be sent automatically.
The "thinking" field is REQUIRED in every response. Omitting it is invalid."""

    def initial_prompt(self, context: dict) -> str:
        """First user message. Must be implemented by each step."""
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement initial_prompt()"
        )

    def validate_complete(self, findings: dict) -> bool:
        """
        Return False to force the model to keep going.
        Override in steps that have mandatory fields.
        """
        return True

    def run(self, context: dict) -> StepResult:
        if not self.cfg.enabled:
            return StepResult(self.name, True, {}, "Step disabled.", 0)

        default_debug_logger.log_event(
            "step_start", step=self.name, max_iterations=self.cfg.max_iterations
        )

        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": self.initial_prompt(context)},
        ]
        thinking_trace: list[str] = []
        blockers: list[dict] = []

        last_failed_signature: tuple[str, str] | None = None
        search_state = SearchState()

        for i in range(self.cfg.max_iterations):
            iterations_left = self.cfg.max_iterations - i - 1

            default_debug_logger.log_event(
                "iteration_start",
                step=self.name,
                iteration=i + 1,
                iterations_left=iterations_left,
            )

            with RollingWindow(max_lines=6) as window:
                raw = self.llm.chat(
                    messages, stream=True, window=window, step_name=self.name
                )

                parsed = _extract_json(raw)
                messages.append({"role": "assistant", "content": raw})

                if parsed and parsed.get("thinking"):
                    thinking_trace.append(parsed["thinking"])

                if parsed is None:
                    default_debug_logger.log_event(
                        "invalid_json", step=self.name, iteration=i + 1, raw=raw
                    )
                    messages.append({"role": "user", "content": _INVALID_JSON_PROMPT})
                    continue

                status = parsed.get("status")

                if status == "failed":
                    default_debug_logger.log_event(
                        "step_failed",
                        step=self.name,
                        iteration=i + 1,
                        error=parsed.get("error"),
                        reason=parsed.get("reason"),
                    )
                    return StepResult(
                        step_name=self.name,
                        success=False,
                        findings={
                            "error": {
                                "message": parsed.get("error", "unknown error"),
                                "reason": parsed.get("reason", "unknown failure"),
                            }
                        },
                        summary=f"[FAILED] {parsed.get('reason', '')}",
                        iterations_used=i + 1,
                        messages=messages,
                        thinking_trace=thinking_trace,
                        blockers=blockers,
                    )

                if status == "blocked":
                    blocker = {
                        "issue": parsed.get("issue", ""),
                        "suggested_fix": parsed.get("suggested_fix", ""),
                        "fallback_action": parsed.get("fallback_action"),
                        "can_continue": parsed.get("can_continue", False),
                    }
                    blockers.append(blocker)
                    default_debug_logger.log_event(
                        "step_blocked", step=self.name, iteration=i + 1, blocker=blocker
                    )

                    fallback = blocker.get("fallback_action")
                    if fallback and blocker.get("can_continue"):
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    f"That blocker has been logged and the user will be notified.\n"
                                    f"You indicated a fallback is available: '{fallback}'.\n"
                                    f"Proceed with the fallback now."
                                ),
                            }
                        )
                        last_failed_signature = None
                        continue

                    return StepResult(
                        step_name=self.name,
                        success=False,
                        findings={"blocked": blocker},
                        summary=f"[BLOCKED] {blocker['issue']}. Fix: {blocker['suggested_fix']}",
                        iterations_used=i + 1,
                        messages=messages,
                        thinking_trace=thinking_trace,
                        blockers=blockers,
                    )

                if status == "complete":
                    findings = parsed.get("findings", {})
                    if self.validate_complete(findings):
                        default_debug_logger.log_event(
                            "step_complete",
                            step=self.name,
                            iteration=i + 1,
                            summary=parsed.get("summary", ""),
                            findings=findings,
                        )
                        return StepResult(
                            step_name=self.name,
                            success=True,
                            findings=findings,
                            summary=parsed.get("summary", ""),
                            iterations_used=i + 1,
                            messages=messages,
                            thinking_trace=thinking_trace,
                            blockers=blockers,
                        )
                    default_debug_logger.log_event(
                        "incomplete_findings",
                        step=self.name,
                        iteration=i + 1,
                        findings=findings,
                    )
                    messages.append(
                        {"role": "user", "content": _INCOMPLETE_FINDINGS_PROMPT}
                    )
                    continue

                if status == "tool_call":
                    tool = parsed.get("tool", "")
                    args = parsed.get("args", "")
                    signature = (tool, args)

                    if signature == last_failed_signature:
                        default_debug_logger.log_event(
                            "repeat_call_blocked",
                            step=self.name,
                            iteration=i + 1,
                            tool=tool,
                            args=args,
                        )
                        messages.append(
                            {
                                "role": "user",
                                "content": _REPEAT_CALL_PROMPT.format(
                                    tool=tool,
                                    args=args,
                                    iterations_left=iterations_left,
                                ),
                            }
                        )
                        continue

                    default_debug_logger.log_event(
                        "tool_call_issued",
                        step=self.name,
                        iteration=i + 1,
                        tool=tool,
                        args=args,
                        reason=parsed.get("reason", ""),
                    )

                    result = dispatch(
                        tool,
                        args,
                        self.project_path,
                        window=window,
                        search_state=search_state,
                    )

                    tool_payload = {
                        "tool": tool,
                        "args": args,
                        "success": result.success,
                        "returncode": result.returncode,
                        "stdout": result.stdout[:10_000],
                        "stderr": result.stderr[:3_000],
                        "system_note": f"{iterations_left} iterations remaining.",
                    }
                    messages.append(
                        {"role": "user", "content": json.dumps(tool_payload, indent=2)}
                    )

                    if result.success:
                        last_failed_signature = None
                        continue

                    last_failed_signature = signature
                    messages.append(
                        {
                            "role": "user",
                            "content": _FAILURE_RETRY_PROMPT.format(
                                returncode=result.returncode,
                                stderr=result.stderr[:1_500],
                                iterations_left=iterations_left,
                                suggested_query=_suggest_search_query(result.stderr),
                            ),
                        }
                    )
                    continue

                default_debug_logger.log_event(
                    "unknown_status", step=self.name, iteration=i + 1, status=status
                )
                messages.append(
                    {
                        "role": "user",
                        "content": _UNKNOWN_STATUS_PROMPT.format(status=status),
                    }
                )

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
        )
