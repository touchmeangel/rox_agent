from ignite_agent.steps.base import BaseStep
from ignite_agent.steps.config import StepConfig
from ignite_agent.steps.report import FinalReportStep
from ignite_agent.steps.static_analysis import StaticAnalysisStep

# The active pipeline, using direct class references
PIPELINE: list[BaseStep] = [
    StaticAnalysisStep(
        StepConfig(
            model_key="reasoning",
            max_iterations=16,
        )
    ),
    FinalReportStep(
        StepConfig(
            model_key="reasoning",
            max_iterations=8,
        )
    ),
]
