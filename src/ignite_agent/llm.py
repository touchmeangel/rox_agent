from __future__ import annotations

import os

from rich.console import Console

STREAM_STYLE = "#706d6b"


class LLMClient:
    def __init__(self, model_key: str):
        from ignite_agent.config import MODELS

        if model_key not in MODELS:
            raise ValueError(f"Unknown model key '{model_key}'.")
        self.cfg = MODELS[model_key]
        self.console = Console()

    def chat(self, messages: list[dict], stream: bool = True) -> str:
        match self.cfg.provider:
            case "ollama":
                return self._openai_compat(
                    messages,
                    stream,
                    base_url=f"{self.cfg.base_url}/v1",
                    api_key="ollama",
                )
            case "openai":
                return self._openai_compat(
                    messages,
                    stream,
                    base_url=None,
                    api_key=os.environ.get("OPENAI_API_KEY", ""),
                )
            case "google":
                return self._openai_compat(
                    messages,
                    stream,
                    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                    api_key=os.environ.get("GOOGLE_API_KEY", ""),
                )
            case "anthropic":
                return self._anthropic(messages, stream)
            case _:
                raise ValueError(f"Unknown provider: {self.cfg.provider}")

    def _stream_render(self, delta_content: str, buffer: list[str]) -> str:
        """Processes and prints data inside an eye-safe, tanned sliding window."""
        import sys

        from rich.markup import escape

        buffer.append(delta_content)

        safe_content = escape(delta_content)

        self.console.print(f"[{STREAM_STYLE}]{safe_content}[/]", end="")

        sys.stdout.flush()

        return delta_content

    def _openai_compat(
        self, messages: list[dict], stream: bool, base_url: str | None, api_key: str
    ) -> str:
        import sys

        from openai import OpenAI

        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        client = OpenAI(**kwargs)

        resp = client.chat.completions.create(
            model=self.cfg.model,
            messages=messages,
            temperature=self.cfg.temperature,
            max_tokens=self.cfg.max_tokens,
            stream=stream,
        )

        if stream:
            full = ""
            for chunk in resp:
                if chunk.choices and chunk.choices[0].delta.content:
                    delta = chunk.choices[0].delta.content
                    full += delta

                    sys.stdout.write(f"\033[38;5;245m{delta}\033[0m")
                    sys.stdout.flush()

            print()
            return full

        return resp.choices[0].message.content or ""

    # ── Anthropic ─────────────────────────────────────────────────────────────

    def _anthropic(self, messages: list[dict], stream: bool) -> str:
        import anthropic

        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise OSError("ANTHROPIC_API_KEY is not set")

        client = anthropic.Anthropic(api_key=key)
        system, filtered = "", []
        for m in messages:
            if m["role"] == "system" and not system:
                system = m["content"]
            else:
                filtered.append(m)

        if stream:
            full = ""
            history_buffer = []
            with client.messages.stream(
                model=self.cfg.model,
                max_tokens=self.cfg.max_tokens,
                system=system,
                messages=filtered,
                temperature=self.cfg.temperature,
            ) as s:
                for text in s.text_stream:
                    self._stream_render(text, history_buffer)
                    full += text
            print()
            return full

        resp = client.messages.create(
            model=self.cfg.model,
            max_tokens=self.cfg.max_tokens,
            system=system,
            messages=filtered,
            temperature=self.cfg.temperature,
        )
        return resp.content[0].text


def get_llm(model_key: str) -> LLMClient:
    return LLMClient(model_key)
