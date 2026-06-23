from dataclasses import dataclass


@dataclass
class StepConfig:
    model_key: str
    max_iterations: int
    max_web_calls: int = 6
    enabled: bool = True
