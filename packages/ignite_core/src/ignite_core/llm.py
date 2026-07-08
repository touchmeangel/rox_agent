from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field

from ignite_core.debug_logger import DebugLogger, default_debug_logger
from ignite_core.model_config import ModelConfig
from ignite_core.reporting import NullReporter, Reporter
from ignite_core.tool_call_parser import ToolCall, parse_text_tool_calls

_INTERLEAVED_THINKING_BETA = "interleaved-thinking-2025-05-14"


def _draw(pool: list[ModelConfig]) -> ModelConfig:
    weights = [max(cfg.alloy_weight, 0.0) for cfg in pool]
    if not any(weights):
        weights = None
    return random.choices(pool, weights=weights, k=1)[0]  # noqa: S311 This is simple random selection, not security-sensitive


def _is_transient(exc: Exception) -> bool:
    # import openai as _openai_mod
    # import anthropic as _anthropic_mod
    # try:
    #     if isinstance(exc, _openai_mod.RateLimitError):      return True
    #     if isinstance(exc, _openai_mod.APIConnectionError):  return True
    #     if isinstance(exc, _openai_mod.InternalServerError): return True
    #     if isinstance(exc, _openai_mod.BadRequestError): return True
    # except Exception:
    #     pass
    # try:
    #     if isinstance(exc, _anthropic_mod.RateLimitError):      return True
    #     if isinstance(exc, _anthropic_mod.APIConnectionError):  return True
    #     if isinstance(exc, _anthropic_mod.InternalServerError): return True
    # except Exception:
    #     pass
    # if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
    #     return True
    # Each provider handle them differently so it would be a headache
    return True


@dataclass
class ToolResultEntry:
    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass
class AssistantTurn:
    text: str = ""
    thinking: str | None = None
    thinking_char_count: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""
    raw: object = None


class LLMClient:
    def __init__(self, chain: list[ModelConfig]):
        if not chain:
            raise ValueError("LLMClient requires at least one ModelConfig in `chain`.")
        self.chain: list[ModelConfig] = chain

    @staticmethod
    def thinking_budget(cfg: ModelConfig) -> int | None:
        return getattr(cfg, "thinking_budget", None)

    @property
    def cfg(self) -> ModelConfig:
        return self.chain[0]

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
        *,
        reporter: Reporter | None = None,
        step_name: str = "",
        debug_logger: DebugLogger | None = None,
        show_thinking: bool = False,
    ) -> AssistantTurn:
        reporter = reporter or NullReporter()
        debug_logger = debug_logger or default_debug_logger
        last_exc: Exception | None = None
        attempted: list[str] = []
        remaining = list(self.chain)
        previous_model: str | None = None

        while remaining:
            cfg = _draw(remaining)
            remaining.remove(cfg)
            attempted.append(cfg.model)

            if previous_model is not None:
                reporter.model_fallback(previous_model, cfg.model, last_exc)
            else:
                reporter.model_selected(cfg.model)
            previous_model = cfg.model

            thinking_enabled = bool(getattr(cfg, "thinking_budget", None))
            spinner_msg = "Extended thinking…" if thinking_enabled else "Thinking…"
            reporter.start_spinner(spinner_msg)

            start = time.time()

            try:
                api_key = self._resolve_api_key(cfg)

                match cfg.provider:
                    case "openai":
                        turn = self._openai_with_tools(
                            cfg,
                            messages,
                            tools,
                            system,
                            reporter,
                            base_url=cfg.base_url,
                            api_key=api_key,
                            show_thinking=show_thinking,
                        )
                    case "anthropic":
                        turn = self._anthropic_with_tools(
                            cfg,
                            messages,
                            tools,
                            system,
                            reporter,
                            api_key=api_key,
                            show_thinking=show_thinking,
                        )
                    case _:
                        raise ValueError(f"Unknown provider: {cfg.provider}")

                debug_logger.log_llm_call(
                    step=step_name,
                    provider=cfg.provider,
                    model=cfg.model,
                    messages=messages,
                    response=_log_repr(turn),
                    duration_s=time.time() - start,
                )
                return turn
            except Exception as exc:
                reporter.stop_spinner()
                debug_logger.log_llm_call(
                    step=step_name,
                    provider=cfg.provider,
                    model=cfg.model,
                    messages=messages,
                    response="",
                    duration_s=time.time() - start,
                    error=repr(exc),
                )
                debug_logger.log_event(
                    "llm_call_error",
                    step=step_name,
                    provider=cfg.provider,
                    model=cfg.model,
                    error_type=type(exc).__name__,
                    error=repr(exc),
                )
                last_exc = exc

                if remaining:
                    if _is_transient(exc):
                        continue
                    # Non-transient error with models still left in the pool:
                    # surface it immediately, raw, since it's not a
                    # capacity/availability problem — it's a real bug.
                    raise

                # Pool exhausted — every configured model has now been tried
                # and failed. Raise one clean, catchable error instead of
                # letting the last provider's raw exception (e.g. a bare
                # openai.RateLimitError) propagate uncaught to main().
                debug_logger.log_event(
                    "alloy_pool_exhausted",
                    step=step_name,
                    attempted_models=attempted,
                    last_error_type=type(exc).__name__,
                    last_error=str(exc),
                )
                raise RuntimeError(
                    f"Alloy pool exhausted for step '{step_name}': all "
                    f"{len(attempted)} model(s) failed "
                    f"({', '.join(attempted)}). "
                    f"Last error ({type(exc).__name__}): {exc}"
                ) from exc

        # Unreachable: the while-loop above always either returns a turn or
        # raises before falling through here.
        raise RuntimeError("Alloy pool exhausted") from last_exc

    @staticmethod
    def _resolve_api_key(cfg: ModelConfig) -> str:
        api_key_env = cfg.api_key_env
        if not api_key_env:
            fallback_map = {
                "openai": "OPENAI_API_KEY",
                "anthropic": "ANTHROPIC_API_KEY",
            }
            api_key_env = fallback_map.get(cfg.provider)
        key = os.environ.get(api_key_env, "") if api_key_env else ""
        if not key and cfg.base_url:
            raise RuntimeError(f'no api key for "{cfg.model}" model was provided')
        return key

    def _anthropic_with_tools(
        self,
        cfg: ModelConfig,
        messages,
        tools,
        system,
        reporter: Reporter,
        *,
        api_key,
        show_thinking=False,
    ) -> AssistantTurn:
        from anthropic import Anthropic

        if not api_key:
            raise OSError("Anthropic API identifier key target is empty or unset.")
        client = Anthropic(api_key=api_key)

        kwargs: dict = {
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "temperature": cfg.temperature,
            "system": system,
            "messages": _to_anthropic_messages(messages),
            "tools": _to_anthropic_tools(tools),
        }
        extra_headers: dict = {}
        thinking_budget = LLMClient.thinking_budget(cfg)
        if thinking_budget:
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": thinking_budget,
            }
            kwargs["max_tokens"] = max(cfg.max_tokens, thinking_budget + 2048)
            extra_headers["anthropic-beta"] = _INTERLEAVED_THINKING_BETA

        text_parts: list[str] = []
        thinking_parts: list[str] = []
        spinner_stopped = False
        current_block_type: str | None = None

        with client.messages.stream(extra_headers=extra_headers, **kwargs) as stream:
            for event in stream:
                if not spinner_stopped:
                    reporter.stop_spinner()
                    spinner_stopped = True

                if event.type == "content_block_start":
                    current_block_type = getattr(event.content_block, "type", None)
                    if current_block_type == "thinking" and show_thinking:
                        reporter.thinking_start()
                    continue

                if event.type == "content_block_stop":
                    if current_block_type == "thinking" and show_thinking:
                        char_count = sum(len(p) for p in thinking_parts)
                        reporter.thinking_end(char_count)
                    current_block_type = None
                    continue

                if event.type != "content_block_delta":
                    continue

                delta = event.delta
                if delta.type == "text_delta":
                    text_parts.append(delta.text)
                    reporter.feed(delta.text)
                elif delta.type == "thinking_delta":
                    thinking_parts.append(delta.thinking)
                    if show_thinking:
                        reporter.thinking_delta(delta.thinking)

            final = stream.get_final_message()

        thinking_joined = "".join(thinking_parts)
        tool_calls = [
            ToolCall(id=b.id, name=b.name, input=b.input)
            for b in final.content
            if b.type == "tool_use"
        ]
        return AssistantTurn(
            text="".join(text_parts),
            thinking=thinking_joined or None,
            thinking_char_count=len(thinking_joined),
            tool_calls=tool_calls,
            stop_reason=final.stop_reason or "",
            raw=[block.model_dump() for block in final.content],
        )

    def _openai_with_tools(
        self,
        cfg: ModelConfig,
        messages,
        tools,
        system,
        reporter: Reporter,
        *,
        base_url,
        api_key,
        show_thinking=False,
    ) -> AssistantTurn:
        from openai import OpenAI

        kwargs: dict = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        client = OpenAI(**kwargs)

        call_kwargs: dict = {
            "model": cfg.model,
            "messages": _to_openai_messages(messages, system),
            "tools": _to_openai_tools(tools),
            "tool_choice": "auto",
            "max_tokens": cfg.max_tokens,
            "stream": True,
        }

        if cfg.temperature:
            call_kwargs["temperature"] = cfg.temperature

        if cfg.reasoning_effort:
            call_kwargs["reasoning_effort"] = cfg.reasoning_effort

        stream = client.chat.completions.create(**call_kwargs)

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_call_slots: dict[int, dict] = {}
        finish_reason = ""
        spinner_stopped = False
        thinking_box_open = False

        for chunk in stream:
            if not spinner_stopped:
                reporter.stop_spinner()
                spinner_stopped = True
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            delta = choice.delta

            reasoning = getattr(delta, "reasoning_content", None) or getattr(
                delta, "reasoning", None
            )
            if reasoning:
                reasoning_parts.append(reasoning)
                if show_thinking:
                    if not thinking_box_open:
                        reporter.thinking_start()
                        thinking_box_open = True
                    reporter.thinking_delta(reasoning)

            if delta.content:
                if thinking_box_open and show_thinking:
                    char_count = sum(len(p) for p in reasoning_parts)
                    reporter.thinking_end(char_count)
                    thinking_box_open = False
                text_parts.append(delta.content)
                reporter.feed(delta.content)

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

        if thinking_box_open and show_thinking:
            char_count = sum(len(p) for p in reasoning_parts)
            reporter.thinking_end(char_count)

        tool_calls: list[ToolCall] = []
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

        text = "".join(text_parts)
        if not tool_calls and text:
            parsed = parse_text_tool_calls(text)
            if parsed:
                tool_calls = parsed
                text = ""

        raw_tool_calls = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.input),
                },
            }
            for tc in tool_calls
        ]

        raw_assistant_message: dict = {"role": "assistant", "content": text or ""}
        if raw_tool_calls:
            raw_assistant_message["tool_calls"] = raw_tool_calls

        reasoning_joined = "".join(reasoning_parts)
        return AssistantTurn(
            text=text,
            thinking=reasoning_joined or None,
            thinking_char_count=len(reasoning_joined),
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
                raw = dict(entry["raw"])
                if raw.get("tool_calls") is None:
                    raw.pop("tool_calls", None)
                if raw.get("content") is None:
                    raw["content"] = ""
                out.append(raw)
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
    parts: list[str] = []

    if turn.thinking:
        snippet = turn.thinking.replace("\n", " ").strip()
        if len(snippet) > 200:
            snippet = snippet[:200] + f"… ({turn.thinking_char_count} chars total)"
        parts.append(f"[thinking]: {snippet}")

    if turn.tool_calls:
        calls = ", ".join(f"{c.name}({c.input})" for c in turn.tool_calls)
        parts.append(f"[tool_calls: {calls}]")

    if turn.text:
        parts.append(turn.text)

    return "\n".join(parts) if parts else ""


def get_llm(*configs: ModelConfig) -> LLMClient:
    return LLMClient(chain=list(configs))
