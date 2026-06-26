from ignite_agent.steps.base import BaseStep
from ignite_agent.steps.config import StepConfig
from ignite_agent.steps.documentation import DocumentationStep
from ignite_agent.steps.report import FinalReportStep
from ignite_agent.steps.static_analysis import StaticAnalysisStep

# The active pipeline, using direct class references
PIPELINE: list[BaseStep] = [
    DocumentationStep(
        StepConfig(model_key="reasoning", max_iterations=16, max_web_calls=12)
    ),
    StaticAnalysisStep(
        StepConfig(
            model_key="reasoning",
            max_iterations=32,
        )
    ),
    FinalReportStep(
        StepConfig(
            model_key="reasoning",
            max_iterations=16,
        )
    ),
]
