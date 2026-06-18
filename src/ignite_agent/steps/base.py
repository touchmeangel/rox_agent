"""
BaseStep: the agentic loop every audit step inherits.

How it works
────────────
1.  Send system prompt + initial user prompt to the model.
2.  Model must reply with JSON in one of two shapes:

    Tool call →   {"status": "tool_call",  "tool": "...", "args": "...", "reason": "..."}
    Done      →   {"status": "complete",   "findings": {...},            "summary": "..."}

3.  On tool_call: run the tool, append result, go back to 2.
4.  On complete (or max_iterations): return StepResult.

This is the same "observe → think → act" loop Claude Code uses.
The model drives; the loop just executes what it asks for.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ignite_agent.config import PROJECT_PATH, STEPS, StepConfig
from ignite_agent.llm import LLMClient, get_llm
from ignite_agent.tools import TOOL_DESCRIPTIONS, dispatch


@dataclass
class StepResult:
    step_name: str
    success: bool
    findings: dict
    summary: str
    iterations_used: int
    messages: list[dict] = field(default_factory=list)


def _extract_json(text: str) -> dict | None:
    """Pull the first valid JSON object out of a model response."""
    # 1. Direct parse (model did the right thing)
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    # 2. Fenced code block
    m = re.search(r"```(?:json)?\s*(\{.*?})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 3. Bare JSON object anywhere in the text
    m = re.search(r"\{.*}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


class BaseStep:
    """
    Subclass this, set `name`, implement `initial_prompt`.
    Optionally override `system_prompt` or `validate_complete`.
    """

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
        return f"""You are an expert Solidity / EVM security auditor.
The project under audit is at: {self.project_path}

Available tools:
{tools_block}

You MUST respond with exactly one JSON object — no prose, no markdown outside the JSON.
Two allowed shapes:

  Run a tool:
  {{
    "status": "tool_call",
    "tool":   "<tool_name>",
    "args":   "<arguments string>",
    "reason": "<one sentence: why you need this>"
  }}

  Finished this step:
  {{
    "status":   "complete",
    "findings": {{ ... }},
    "summary":  "<concise paragraph>"
  }}

  Step failed:
  {{
    "status": "failed",
    "error": "...",
    "reason": "..."
  }}

Invalid JSON or unknown status will cause a retry prompt."""

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
            print(f"[{self.name}] skipped (disabled in config)")
            return StepResult(self.name, True, {}, "Step disabled.", 0)

        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": self.initial_prompt(context)},
        ]

        print(f"\n{'─' * 60}")
        print(
            f"[{self.name}] starting  model={self.cfg.model_key}  max_iter={self.cfg.max_iterations}"
        )

        for i in range(self.cfg.max_iterations):
            print(f"\n[{self.name}] iteration {i + 1}/{self.cfg.max_iterations}")

            raw = self.llm.chat(messages, stream=True)
            parsed = _extract_json(raw)

            messages.append({"role": "assistant", "content": raw})

            if parsed is None:
                print(
                    f"[{self.name}] ⚠  response was not valid JSON — asking model to retry"
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your response was not valid JSON. "
                            "Reply with only a JSON object matching one of the two allowed shapes."
                        ),
                    }
                )
                continue

            status = parsed.get("status")

            if status == "failed":
                reason = parsed.get("reason", "unknown failure")
                error = parsed.get("error", "unknown error")

                return StepResult(
                    step_name=self.name,
                    success=False,
                    findings={"error": {"message": error, "reason": reason}},
                    summary=f"[FAILED] {reason}",
                    iterations_used=i + 1,
                    messages=messages,
                )

            if status == "complete":
                findings = parsed.get("findings", {})
                if self.validate_complete(findings):
                    print(f"[{self.name}] ✓ complete after {i + 1} iteration(s)")
                    return StepResult(
                        step_name=self.name,
                        success=True,
                        findings=findings,
                        summary=parsed.get("summary", ""),
                        iterations_used=i + 1,
                        messages=messages,
                    )
                messages.append(
                    {
                        "role": "user",
                        "content": "The findings are incomplete. Continue your analysis.",
                    }
                )
                continue

            if status == "tool_call":
                tool = parsed.get("tool", "")
                args = parsed.get("args", "")
                reason = parsed.get("reason", "")
                print(f"[{self.name}] tool={tool!r}  args={args!r}")
                print(f"[{self.name}] reason: {reason}")

                result = dispatch(tool, args, self.project_path)
                tool_payload = {
                    "tool": tool,
                    "args": args,
                    "success": result.success,
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }

                messages.append(
                    {"role": "user", "content": json.dumps(tool_payload, indent=2)}
                )
                if not result.success:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "TOOL EXECUTION FAILED.\n"
                                "You must now choose ONE:\n"
                                "- retry tool with corrected args\n"
                                "- explicitly conclude analysis is incomplete\n"
                            ),
                        }
                    )

                continue

            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Unknown status value '{status}'. "
                        "Use 'tool_call' to run a tool, or 'complete' when done."
                    ),
                }
            )

        # Max iterations exhausted
        print(f"[{self.name}] ⚠  max iterations reached without completing")
        return StepResult(
            step_name=self.name,
            success=False,
            findings={"warning": "max_iterations_reached"},
            summary="Step hit the iteration limit before finishing.",
            iterations_used=self.cfg.max_iterations,
            messages=messages,
        )
