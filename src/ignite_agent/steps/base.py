from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ignite_agent.config import PROJECT_PATH, STEPS, StepConfig
from ignite_agent.llm import LLMClient, RollingWindow, get_llm
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
TOOL EXECUTION FAILED (exit {returncode}).

─── stderr ──────────────────────────────────
{stderr}
─────────────────────────────────────────────

Use your "thinking" field to work through this systematically:

  OBSERVE:  What is the exact error? (permissions? bad args? missing dependency? env issue?)
  ANALYZE:  Is this recoverable without user action?
  OPTIONS:  List every path forward:
            • retry with corrected args
            • use a different tool or command
            • web_search the specific error message
            • use shell to fix the environment (chmod, mkdir, pip install, export HOME=…)
  DECISION: Pick the best option. Do not give up until you have tried at least 2 alternatives.

If — and only if — the error requires the user to act (e.g. missing API key, sudo required),
use status "blocked" with an exact `suggested_fix` and a `fallback_action` if one exists.
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
            What do I know now that I didn't before?
  ANALYZE:  What does this mean? Is it a real issue or noise?
            Are there conflicting signals?
  PLAN:     What are my options? Which is best? Why?
  ACT:      What am I about to do? Exactly what args / fields?

When a tool fails:
  • Do NOT immediately give up.
  • Think about root cause first.
  • Try at least 2 alternatives before declaring blocked or failed.
  • Use web_search when you need to understand an error you haven't seen before.

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

        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": self.initial_prompt(context)},
        ]

        thinking_trace: list[str] = []
        blockers: list[dict] = []

        for i in range(self.cfg.max_iterations):
            with RollingWindow(max_lines=6) as window:
                raw = self.llm.chat(messages, stream=True, window=window)

                parsed = _extract_json(raw)
                messages.append({"role": "assistant", "content": raw})

                if parsed and parsed.get("thinking"):
                    thinking_trace.append(parsed["thinking"])

                if parsed is None:
                    messages.append({"role": "user", "content": _INVALID_JSON_PROMPT})
                    continue

                status = parsed.get("status")

                if status == "failed":
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
                    messages.append(
                        {"role": "user", "content": _INCOMPLETE_FINDINGS_PROMPT}
                    )
                    continue

                if status == "tool_call":
                    tool = parsed.get("tool", "")
                    args = parsed.get("args", "")

                    result = dispatch(tool, args, self.project_path, window=window)

                    tool_payload = {
                        "tool": tool,
                        "args": args,
                        "success": result.success,
                        "returncode": result.returncode,
                        "stdout": result.stdout[:10_000],
                        "stderr": result.stderr[:3_000],
                    }
                    messages.append(
                        {"role": "user", "content": json.dumps(tool_payload, indent=2)}
                    )

                    if not result.success:
                        messages.append(
                            {
                                "role": "user",
                                "content": _FAILURE_RETRY_PROMPT.format(
                                    returncode=result.returncode,
                                    stderr=result.stderr[:1_500],
                                ),
                            }
                        )
                    continue

                messages.append(
                    {
                        "role": "user",
                        "content": _UNKNOWN_STATUS_PROMPT.format(status=status),
                    }
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
