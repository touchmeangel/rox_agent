from __future__ import annotations

import os

from ignite_agent.rolling_window import RollingWindow


class LLMClient:
    def __init__(self, model_key: str):
        from ignite_agent.config import MODELS

        if model_key not in MODELS:
            raise ValueError(f"Unknown model key '{model_key}'.")
        self.cfg = MODELS[model_key]

    def chat(
        self,
        messages: list[dict],
        stream: bool = True,
        window: RollingWindow | None = None,
    ) -> str:
        if window is not None:
            window.start_spinner("Thinking...")

        try:
            match self.cfg.provider:
                case "ollama":
                    return self._openai(
                        messages,
                        stream,
                        window,
                        base_url=f"{self.cfg.base_url}/v1",
                        api_key="ollama",
                    )
                case "openai":
                    return self._openai(
                        messages,
                        stream,
                        window,
                        base_url=self.cfg.base_url,
                        api_key=os.environ.get("OPENAI_API_KEY", ""),
                    )
                case "google":
                    return self._openai(
                        messages,
                        stream,
                        window,
                        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                        api_key=os.environ.get("GOOGLE_API_KEY", ""),
                    )
                case "anthropic":
                    return self._anthropic(messages, stream, window)
                case _:
                    raise ValueError(f"Unknown provider: {self.cfg.provider}")
        except Exception:
            if window is not None:
                window.stop_spinner()
            raise

    def _render_stream(
        self,
        chunk_generator,
        window: RollingWindow | None = None,
        max_lines: int = 4,
    ) -> str:
        full = ""
        iterator = iter(chunk_generator)

        if window is not None:
            try:
                first_chunk = next(iterator, None)
            except StopIteration:
                first_chunk = None
            finally:
                window.stop_spinner()

            if first_chunk:
                full += first_chunk
                window.feed(first_chunk)
                for chunk in iterator:
                    if chunk:
                        full += chunk
                        window.feed(chunk)
            return full

        with RollingWindow(max_lines) as local_window:
            for chunk in chunk_generator:
                if not chunk:
                    continue
                full += chunk
                local_window.feed(chunk)
        return full

    def _openai(
        self,
        messages: list[dict],
        stream: bool,
        window: RollingWindow | None,
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

            return self._render_stream(_gen(), window)

        return resp.choices[0].message.content or ""

    def _anthropic(
        self, messages: list[dict], stream: bool, window: RollingWindow | None
    ) -> str:
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

            return self._render_stream(_gen(), window)

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
