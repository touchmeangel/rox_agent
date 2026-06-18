from ignite_agent.steps.report import FinalReportStep
from ignite_agent.steps.static_analysis import StaticAnalysisStep

# The active pipeline, using direct class references
PIPELINE: list[type] = [
    StaticAnalysisStep,
    FinalReportStep,
]