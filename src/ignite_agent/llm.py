from __future__ import annotations

import os

from ignite_agent.config import MODELS, ModelConfig


class LLMClient:
    def __init__(self, model_key: str):
        if model_key not in MODELS:
            raise ValueError(
                f"Unknown model key '{model_key}'. Check config.json models."
            )
        self.cfg: ModelConfig = MODELS[model_key]

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

    # ── OpenAI-compatible (ollama / openai / google) ──────────────────────────

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
            full = ""
            for chunk in resp:
                delta = chunk.choices[0].delta.content
                if delta:
                    print(delta, end="", flush=True)
                    full += delta
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
            with client.messages.stream(
                model=self.cfg.model,
                max_tokens=self.cfg.max_tokens,
                system=system,
                messages=filtered,
                temperature=self.cfg.temperature,
            ) as s:
                for text in s.text_stream:
                    print(text, end="", flush=True)
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
