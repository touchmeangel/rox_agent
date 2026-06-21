from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from ignite_agent.debug_logger import DebugLogger, default_debug_logger
from ignite_agent.rolling_window import RollingWindow

_INTERLEAVED_THINKING_BETA = "interleaved-thinking-2025-05-14"


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class ToolResultEntry:
    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass
class AssistantTurn:
    text: str = ""
    thinking: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""
    raw: object = None


class LLMClient:
    def __init__(self, model_key: str):
        from ignite_agent.config import MODELS

        if model_key not in MODELS:
            raise ValueError(f"Unknown model key '{model_key}'.")
        self.cfg = MODELS[model_key]
        self.thinking_budget: int | None = getattr(self.cfg, "thinking_budget", None)

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
        *,
        window: RollingWindow | None = None,
        step_name: str = "",
        debug_logger: DebugLogger | None = None,
    ) -> AssistantTurn:
        debug_logger = debug_logger or default_debug_logger

        if window is not None:
            window.start_spinner("Thinking...")

        # 1. Resolve which environment variable key this model profile targets
        api_key_env = getattr(self.cfg, "api_key_env", None)
        if not api_key_env:
            # Fallback legacy structural assignments
            fallback_map = {
                "openai": "OPENAI_API_KEY",
                "google": "GOOGLE_API_KEY",
                "anthropic": "ANTHROPIC_API_KEY",
            }
            api_key_env = fallback_map.get(self.cfg.provider)

        api_key = os.environ.get(api_key_env, "") if api_key_env else ""

        start = time.time()
        try:
            match self.cfg.provider:
                case "openai":
                    turn = self._openai_with_tools(
                        messages,
                        tools,
                        system,
                        window,
                        base_url=self.cfg.base_url,
                        api_key=api_key,
                    )
                case "anthropic":
                    turn = self._anthropic_with_tools(
                        messages, tools, system, window, api_key=api_key
                    )
                case _:
                    raise ValueError(f"Unknown provider: {self.cfg.provider}")
        except Exception as exc:
            if window is not None:
                window.stop_spinner()
            debug_logger.log_llm_call(
                step=step_name,
                provider=self.cfg.provider,
                model=self.cfg.model,
                messages=messages,
                response="",
                duration_s=time.time() - start,
                error=str(exc),
            )
            raise

        debug_logger.log_llm_call(
            step=step_name,
            provider=self.cfg.provider,
            model=self.cfg.model,
            messages=messages,
            response=_log_repr(turn),
            duration_s=time.time() - start,
        )
        return turn

    def _anthropic_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
        window: RollingWindow | None,
        api_key: str,
    ) -> AssistantTurn:
        from anthropic import Anthropic

        if not api_key:
            raise OSError("Anthropic API identifier key target is empty or unset.")
        client = Anthropic(api_key=api_key)

        kwargs: dict = dict(
            model=self.cfg.model,
            max_tokens=self.cfg.max_tokens,
            temperature=self.cfg.temperature,
            system=system,
            messages=_to_anthropic_messages(messages),
            tools=_to_anthropic_tools(tools),
        )
        extra_headers: dict = {}
        if self.thinking_budget:
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.thinking_budget,
            }
            kwargs["max_tokens"] = max(self.cfg.max_tokens, self.thinking_budget + 2048)
            extra_headers["anthropic-beta"] = _INTERLEAVED_THINKING_BETA

        text_parts: list[str] = []
        thinking_parts: list[str] = []
        spinner_stopped = window is None

        with client.messages.stream(extra_headers=extra_headers, **kwargs) as stream:
            for event in stream:
                if not spinner_stopped:
                    window.stop_spinner()
                    spinner_stopped = True
                if event.type != "content_block_delta":
                    continue
                delta = event.delta
                if delta.type == "text_delta":
                    text_parts.append(delta.text)
                    if window is not None:
                        window.feed(delta.text)
                elif delta.type == "thinking_delta":
                    thinking_parts.append(delta.thinking)
                    if window is not None:
                        window.feed(delta.thinking)
            final = stream.get_final_message()

        tool_calls = [
            ToolCall(id=b.id, name=b.name, input=b.input)
            for b in final.content
            if b.type == "tool_use"
        ]
        return AssistantTurn(
            text="".join(text_parts),
            thinking="".join(thinking_parts) or None,
            tool_calls=tool_calls,
            stop_reason=final.stop_reason or "",
            raw=[block.model_dump() for block in final.content],
        )

    def _openai_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
        window: RollingWindow | None,
        *,
        base_url: str | None,
        api_key: str,
    ) -> AssistantTurn:
        from openai import OpenAI

        kwargs: dict = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        client = OpenAI(**kwargs)

        stream = client.chat.completions.create(
            model=self.cfg.model,
            messages=_to_openai_messages(messages, system),
            tools=_to_openai_tools(tools),
            tool_choice="auto",
            temperature=self.cfg.temperature,
            max_tokens=self.cfg.max_tokens,
            stream=True,
        )

        text_parts: list[str] = []
        tool_call_slots: dict[int, dict] = {}
        finish_reason = ""
        spinner_stopped = window is None

        for chunk in stream:
            if not spinner_stopped:
                window.stop_spinner()
                spinner_stopped = True
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            delta = choice.delta
            if delta.content:
                text_parts.append(delta.content)
                if window is not None:
                    window.feed(delta.content)
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    slot = tool_call_slots.setdefault(
                        tc_delta.index, {"id": None, "name": None, "arguments": ""}
                    )
                    if tc_delta.id:
                        slot["id"] = tc_delta.id
                    if tc_delta.function and tc_delta.function.name:
                        slot["name"] = tc_delta.function.name
                    if tc_delta.function and tc_delta.function.arguments:
                        slot["arguments"] += tc_delta.function.arguments

        tool_calls: list[ToolCall] = []
        raw_tool_calls: list[dict] = []
        for idx in sorted(tool_call_slots):
            slot = tool_call_slots[idx]
            try:
                parsed_input = (
                    json.loads(slot["arguments"]) if slot["arguments"] else {}
                )
            except json.JSONDecodeError:
                parsed_input = {"_unparsed_arguments": slot["arguments"]}
            call_id = slot["id"] or f"call_{idx}"
            tool_calls.append(
                ToolCall(id=call_id, name=slot["name"] or "", input=parsed_input)
            )
            raw_tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": slot["name"] or "",
                        "arguments": slot["arguments"],
                    },
                }
            )

        text = "".join(text_parts)
        raw_assistant_message = {
            "role": "assistant",
            "content": text or None,
            "tool_calls": raw_tool_calls or None,
        }

        return AssistantTurn(
            text=text,
            thinking=None,
            tool_calls=tool_calls,
            stop_reason=finish_reason or "stop",
            raw=raw_assistant_message,
        )


def _to_anthropic_messages(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for entry in messages:
        role = entry["role"]
        if role == "user":
            out.append(
                {
                    "role": "user",
                    "content": [{"type": "text", "text": entry["content"]}],
                }
            )
        elif role == "assistant":
            if entry.get("raw") is not None:
                out.append({"role": "assistant", "content": entry["raw"]})
            else:
                blocks: list[dict] = []
                if entry.get("text"):
                    blocks.append({"type": "text", "text": entry["text"]})
                for tc in entry.get("tool_calls", []):
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.input,
                        }
                    )
                out.append({"role": "assistant", "content": blocks})
        elif role == "tool_results":
            blocks = []
            for r in entry["results"]:
                block = {
                    "type": "tool_result",
                    "tool_use_id": r.tool_call_id,
                    "content": r.content,
                }
                if r.is_error:
                    block["is_error"] = True
                blocks.append(block)
            out.append({"role": "user", "content": blocks})
        else:
            raise ValueError(f"Unknown canonical role: {role!r}")
    return out


def _to_anthropic_tools(specs: list[dict]) -> list[dict]:
    return specs


def _to_openai_messages(messages: list[dict], system: str) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}] if system else []
    for entry in messages:
        role = entry["role"]
        if role == "user":
            out.append({"role": "user", "content": entry["content"]})
        elif role == "assistant":
            if entry.get("raw") is not None:
                out.append(entry["raw"])
            else:
                tool_calls = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.input),
                        },
                    }
                    for tc in entry.get("tool_calls", [])
                ]
                out.append(
                    {
                        "role": "assistant",
                        "content": entry.get("text") or None,
                        "tool_calls": tool_calls or None,
                    }
                )
        elif role == "tool_results":
            for r in entry["results"]:
                content = f"ERROR: {r.content}" if r.is_error else r.content
                out.append(
                    {"role": "tool", "tool_call_id": r.tool_call_id, "content": content}
                )
        else:
            raise ValueError(f"Unknown canonical role: {role!r}")
    return out


def _to_openai_tools(specs: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": s["name"],
                "description": s["description"],
                "parameters": s.get(
                    "input_schema", {"type": "object", "properties": {}}
                ),
            },
        }
        for s in specs
    ]


def _log_repr(turn: AssistantTurn) -> str:
    if turn.tool_calls:
        calls = ", ".join(f"{c.name}({c.input})" for c in turn.tool_calls)
        return (
            f"{turn.text}\n[tool_calls: {calls}]"
            if turn.text
            else f"[tool_calls: {calls}]"
        )
    return turn.text


def get_llm(model_key: str) -> LLMClient:
    return LLMClient(model_key)
