from dataclasses import dataclass, field

from ignite_core.model_config import ModelConfig


@dataclass
class StepConfig:
    max_iterations: int
    models: list[ModelConfig] = field(default_factory=list)
    max_web_calls: int = 6