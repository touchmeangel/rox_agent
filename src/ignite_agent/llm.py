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

    def _render_stream(self, chunk_generator) -> str:
        """Streams the LLM output as a single-line sliding window to prevent terminal flooding."""
        import sys

        full_text = ""
        window_size = 100

        for text in chunk_generator:
            if text:
                full_text += text

                flat_text = full_text.replace("\n", " ↵ ")

                display_text = flat_text[-window_size:]

                sys.stdout.write(
                    f"\r  \033[38;5;245m...{display_text:<{window_size}}\033[0m"
                )
                sys.stdout.flush()

        sys.stdout.write("\r\033[K")
        sys.stdout.flush()

        return full_text

    def _openai_compat(
        self, messages: list[dict], stream: bool, base_url: str | None, api_key: str
    ) -> str:
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

            def openai_generator():
                for chunk in resp:
                    if chunk.choices and chunk.choices[0].delta.content:
                        yield chunk.choices[0].delta.content

            return self._render_stream(openai_generator())

        return resp.choices[0].message.content or ""

    # ── Anthropic ─────────────────────────────────────────────────────────────

    def _anthropic_compat(self, messages: list[dict], stream: bool) -> str:
        from anthropic import Anthropic

        client = Anthropic(api_key=self.cfg.api_key)

        system_prompt = next(
            (m["content"] for m in messages if m["role"] == "system"), ""
        )
        api_messages = [m for m in messages if m["role"] != "system"]

        if stream:
            def anthropic_generator():
                with client.messages.stream(
                    model=self.cfg.model,
                    max_tokens=self.cfg.max_tokens,
                    temperature=self.cfg.temperature,
                    system=system_prompt,
                    messages=api_messages,
                ) as stream_obj:
                    yield from stream_obj.text_stream

            return self._render_stream(anthropic_generator())

        resp = client.messages.create(
            model=self.cfg.model,
            max_tokens=self.cfg.max_tokens,
            temperature=self.cfg.temperature,
            system=system_prompt,
            messages=api_messages,
        )
        return resp.content[0].text


def get_llm(model_key: str) -> LLMClient:
    return LLMClient(model_key)
