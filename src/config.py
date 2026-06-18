from __future__ import annotations
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from steps.static_analysis import StaticAnalysisStep
# from steps.report import ReportStep

_CONFIG_PATH = Path(__file__).parent / "config.json"


def _load_raw() -> dict:
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"config.json not found at {_CONFIG_PATH}."
        )
    return json.loads(_CONFIG_PATH.read_text())


@dataclass
class ModelConfig:
    provider: str            # "ollama" | "anthropic"
    model: str
    temperature: float
    max_tokens: int
    base_url: Optional[str] = None


@dataclass
class StepConfig:
    model_key: str          # key in MODELS below
    max_iterations: int = 5
    enabled: bool = True


_raw = _load_raw()

MODELS: dict[str, ModelConfig] = {
    key: ModelConfig(**val)
    for key, val in _raw["models"].items()
}

STEPS: dict[str, StepConfig] = {
    "static_analysis": StepConfig(
        model_key="local",
        max_iterations=4,
    ),
    "pattern_scan": StepConfig(
        model_key="local",
        max_iterations=5,
        enabled=False,  # flip when you implement it
    ),
    "deep_reasoning": StepConfig(
        model_key="reasoning",
        max_iterations=3,
        enabled=False,
    ),
    "report": StepConfig(
        model_key="local",
        max_iterations=2,
        enabled=False,
    ),
}

PIPELINE = [
    StaticAnalysisStep,
    ReportStep,
]

# Runtime path — can be overridden by env var or CLI arg
PROJECT_PATH: str = os.environ.get("PROJECT_PATH", "/project")