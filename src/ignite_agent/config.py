from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

_env_override = os.environ.get("IGNITE_CONFIG_PATH")

if _env_override:
    _CONFIG_PATH = Path(_env_override)
else:
    # Smart Auto-Detection Fallbacks (Lower Priority)
    _POSSIBLE_PATHS = [
        Path("/app/config.json"),
        Path.cwd() / "config.json",
    ]
    _CONFIG_PATH = next((p for p in _POSSIBLE_PATHS if p.exists()), _POSSIBLE_PATHS[0])


def _load_raw() -> dict:
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Configuration file not found at: {_CONFIG_PATH}\n"
            f"Hint: Set the 'IGNITE_CONFIG_PATH' environment variable to point to your config file."
        )
    return json.loads(_CONFIG_PATH.read_text())


@dataclass
class ModelConfig:
    provider: str
    model: str
    temperature: float
    max_tokens: int
    base_url: str | None = None


@dataclass
class StepConfig:
    model_key: str
    max_iterations: int
    enabled: bool = True


STEPS: dict[str, StepConfig] = {
    "static_analysis": StepConfig(
        model_key="reasoning",
        max_iterations=16,
    ),
    "final_report": StepConfig(
        model_key="reasoning",
        max_iterations=8,
    ),
}

_raw = _load_raw()
_models = _raw.get("models", {})
_task_routing = _raw.get("task_routing", {})

MODELS: dict[str, ModelConfig] = {
    key: ModelConfig(**_models.get(val, {})) for key, val in _task_routing.items()
}

PROJECT_PATH: str = os.environ.get("PROJECT_PATH", "/project")
