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


# ── Dataclasses ───────────────────────────────────────────────────────────────
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
        model_key="local",
        max_iterations=16,
    ),
    "final_report": StepConfig(
        model_key="reasoning",
        max_iterations=8,
    ),
}

_raw = _load_raw()

MODELS: dict[str, ModelConfig] = {
    key: ModelConfig(**val) for key, val in _raw.get("models", {}).items()
}

_task_routing = _raw.get("task_routing", {})
_user_default_model = _task_routing.get("default")

for step_name, step_cfg in STEPS.items():
    assigned_model = _task_routing.get(step_name, _user_default_model)

    if assigned_model:
        if assigned_model not in MODELS:
            raise ValueError(
                f"Step '{step_name}' is routed to an undefined model: '{assigned_model}'"
            )
        step_cfg.model_key = assigned_model

PROJECT_PATH: str = os.environ.get("PROJECT_PATH", "/project")
