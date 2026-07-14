from dataclasses import dataclass, field
from typing import Literal

from ignite_core.graph import SlocKind
from ignite_core.model_config import ModelConfig


@dataclass
class StepConfig:
    max_iterations: int = field()
    models: list[ModelConfig] = field(default_factory=list)
    max_web_calls: int = 6
    scales_with_code: bool = False
    sloc_kind: SlocKind | Literal["all"] = "src"
    per_contract: float = 0.0
    per_kloc: float = 0.0
    min_iterations: int = 10
