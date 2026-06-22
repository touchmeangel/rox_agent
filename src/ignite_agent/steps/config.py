from dataclasses import dataclass


@dataclass
class StepConfig:
    model_key: str
    max_iterations: int
    enabled: bool = True
