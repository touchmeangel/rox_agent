"""Loads config.json and manages internal step definitions and pipeline execution order."""
from __future__ import annotations
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Type

from steps.static_analysis import StaticAnalysisStep
from steps.report import FinalReportStep

_CONFIG_PATH = Path(__file__).parent / "config.json"

def _load_raw() -> dict:
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(f"config.json not found at {_CONFIG_PATH}.")
    return json.loads(_CONFIG_PATH.read_text())

# ── Dataclasses ───────────────────────────────────────────────────────────────
@dataclass
class ModelConfig:
    provider: str
    model: str
    temperature: float
    max_tokens: int
    base_url: Optional[str] = None

@dataclass
class StepConfig:
    model_key: str
    max_iterations: int
    enabled: bool = True  # Default to True if not specified

# ── Hardcoded Step Configurations (Base Defaults) ────────────────────────────
STEPS: dict[str, StepConfig] = {
    "static_analysis": StepConfig(
        model_key="local",
        max_iterations=4,
    ),
    "pattern_scan": StepConfig(
        model_key="local",
        max_iterations=5,
        enabled=False,  # Flip when implemented
    ),
    "deep_reasoning": StepConfig(
        model_key="reasoning",
        max_iterations=3,
        enabled=False,
    ),
    "report": StepConfig(
        model_key="local",
        max_iterations=2,
        enabled=True,
    ),
}

# ── Define Execution Order ────────────────────────────────────────────────────
# The active pipeline, using direct class references
PIPELINE: list[Type] = [
    StaticAnalysisStep,
    FinalReportStep,
]

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
            raise ValueError(f"Step '{step_name}' is routed to an undefined model: '{assigned_model}'")
        step_cfg.model_key = assigned_model

PROJECT_PATH: str = os.environ.get("PROJECT_PATH", "/project")