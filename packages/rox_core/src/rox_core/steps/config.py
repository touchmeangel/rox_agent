from dataclasses import dataclass, field

from rox_core.model_config import ModelConfig


@dataclass
class StepConfig:
    max_iterations: int = field()
    models: list[ModelConfig] = field(default_factory=list)
    max_web_calls: int | None = 6
