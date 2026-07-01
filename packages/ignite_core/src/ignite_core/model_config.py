from dataclasses import dataclass

@dataclass(frozen=True)
class ModelConfig:
    provider: str
    model: str
    max_tokens: int
    temperature: float | None = None
    reasoning_effort: str | None = None
    thinking_budget: int | None = None
    base_url: str | None = None
    api_key_env: str | None = None
