from ignite_core.steps.base import BaseStep
from ignite_core.steps.config import StepConfig
from ignite_worker.config import MODELS
from ignite_worker.steps.vulnerability_check import VulnerabilityCheckStep

# The active pipeline, using direct class references
PIPELINE: list[BaseStep] = [
    VulnerabilityCheckStep(
        StepConfig(max_iterations=80, models=MODELS["reasoning"], max_web_calls=12)
    )
]
