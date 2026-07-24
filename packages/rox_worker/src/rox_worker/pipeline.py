from rox_core.steps.base import BaseStep
from rox_core.steps.config import StepConfig
from rox_worker.config import MODELS
from rox_worker.steps.vulnerability_check import VulnerabilityCheckStep

# The active pipeline, using direct class references
PIPELINE: list[BaseStep] = [
    VulnerabilityCheckStep(
        StepConfig(max_iterations=80, models=MODELS["reasoning"], max_web_calls=12)
    )
]
