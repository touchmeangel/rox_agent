from __future__ import annotations
import os
from config import ModelConfig, MODELS


class LLMClient:
    def __init__(self, model_key: str):
        if model_key not in MODELS:
            raise ValueError(f"Unknown model key '{model_key}'. Available: {list(MODELS)}")
        self.key = model_key
        self.cfg: ModelConfig = MODELS[model_key]

    def chat(self, messages: list[dict], stream: bool = True) -> str:
        if self.cfg.provider == "ollama":
            return self._call_ollama(messages, stream)
        if self.cfg.provider == "anthropic":
            return self._call_anthropic(messages, stream)
        raise ValueError(f"Unknown provider: {self.cfg.provider}")

    # ── Ollama ────────────────────────────────────────────────────────────────

    def _call_ollama(self, messages: list[dict], stream: bool) -> str:
        from openai import OpenAI

        client = OpenAI(
            base_url=f"{self.cfg.base_url}/v1",
            api_key="ollama",          # Ollama ignores the key but SDK requires it
        )
        resp = client.chat.completions.create(
            model=self.cfg.model,
            messages=messages,
            temperature=self.cfg.temperature,
            max_tokens=self.cfg.max_tokens,
            stream=stream,
        )
        if stream:
            return self._collect_stream_openai(resp)
        return resp.choices[0].message.content or ""

    # ── Anthropic ─────────────────────────────────────────────────────────────

    def _call_anthropic(self, messages: list[dict], stream: bool) -> str:
        import anthropic

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY is not set")

        client = anthropic.Anthropic(api_key=api_key)

        # Anthropic keeps system outside the messages list
        system, filtered = self._split_system(messages)

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

    @staticmethod
    def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
        """Pull the first system message out; Anthropic needs it separately."""
        system = ""
        rest = []
        for m in messages:
            if m["role"] == "system" and not system:
                system = m["content"]
            else:
                rest.append(m)
        return system, rest

    @staticmethod
    def _collect_stream_openai(stream) -> str:
        full = ""
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                print(delta, end="", flush=True)
                full += delta
        print()
        return full


def get_llm(model_key: str) -> LLMClient:
    return LLMClient(model_key)