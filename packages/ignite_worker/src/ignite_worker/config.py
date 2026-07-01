from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

from ignite_core.model_config import ModelConfig

_env_override = os.environ.get("IGNITE_CONFIG_PATH")

if _env_override:
    _CONFIG_PATH = Path(_env_override)
else:
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


_raw = _load_raw()
_models = _raw.get("models", {})
_task_routing = _raw.get("task_routing", {})

MODELS: dict[str, list[ModelConfig]] = {}
valid_fields = {f.name for f in dataclasses.fields(ModelConfig)}

for key, val in _task_routing.items():
    model_keys = [val] if isinstance(val, str) else val
    chain = []
    for mk in model_keys:
        raw_model_dict = _models.get(mk, {})
        filtered_dict = {k: v for k, v in raw_model_dict.items() if k in valid_fields}
        chain.append(ModelConfig(**filtered_dict))
    MODELS[key] = chain
