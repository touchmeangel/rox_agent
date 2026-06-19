from __future__ import annotations

import os

STREAM_STYLE = "dim"


class RollingWindow:
    def __init__(self, max_lines: int = 4):
        from rich.console import Console

        self.console = Console()
        self.max_lines = max_lines
        self.lines: list[str] = []
        self.cur = ""
        self.live = None

    def _view(self):
        from rich.text import Text

        rows = (self.lines + ([self.cur] if self.cur else []))[-self.max_lines :]
        text = Text.from_markup("\n".join(rows))
        text.style = STREAM_STYLE
        text.no_wrap = True
        text.overflow = "ellipsis"
        return text

    def __enter__(self):
        from rich.live import Live

        self.live = Live(
            self._view(),
            console=self.console,
            auto_refresh=False,
            transient=False,
            vertical_overflow="crop",
        )
        self.live.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.live.__exit__(exc_type, exc, tb)
        self.console.print()
        return False

    def feed(self, text: str) -> None:
        parts = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        self.cur += parts[0]
        for part in parts[1:]:
            self.lines.append(self.cur)
            self.cur = part
        if self.live:
            self.live.update(self._view(), refresh=True)


class LLMClient:
    def __init__(self, model_key: str):
        from ignite_agent.config import MODELS

        if model_key not in MODELS:
            raise ValueError(f"Unknown model key '{model_key}'.")
        self.cfg = MODELS[model_key]

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
                    base_url=self.cfg.base_url,
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

    def _render_stream(self, chunk_generator, max_lines: int = 4) -> str:
        full = ""
        with RollingWindow(max_lines) as window:
            for chunk in chunk_generator:
                if not chunk:
                    continue
                full += chunk
                window.feed(chunk)
        return full

    def _openai_compat(
        self,
        messages: list[dict],
        stream: bool,
        base_url: str | None,
        api_key: str,
    ) -> str:
        from openai import OpenAI

        kwargs: dict = {"api_key": api_key}
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

            def _gen():
                for chunk in resp:
                    if chunk.choices and chunk.choices[0].delta.content:
                        yield chunk.choices[0].delta.content

            return self._render_stream(_gen())

        return resp.choices[0].message.content or ""

    def _anthropic(self, messages: list[dict], stream: bool) -> str:
        from anthropic import Anthropic

        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise OSError("ANTHROPIC_API_KEY is not set")
        client = Anthropic(api_key=key)

        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        filtered = [m for m in messages if m["role"] != "system"]

        if stream:

            def _gen():
                with client.messages.stream(
                    model=self.cfg.model,
                    max_tokens=self.cfg.max_tokens,
                    temperature=self.cfg.temperature,
                    system=system,
                    messages=filtered,
                ) as s:
                    yield from s.text_stream

            return self._render_stream(_gen())

        resp = client.messages.create(
            model=self.cfg.model,
            max_tokens=self.cfg.max_tokens,
            temperature=self.cfg.temperature,
            system=system,
            messages=filtered,
        )
        return resp.content[0].text


def get_llm(model_key: str) -> LLMClient:
    return LLMClient(model_key)
