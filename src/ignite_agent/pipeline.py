from ignite_agent.steps.base import BaseStep
from ignite_agent.steps.config import StepConfig
from ignite_agent.steps.audit_delta import AuditDeltaStep
from ignite_agent.steps.documentation import DocumentationStep
from ignite_agent.steps.fork_analysis import ForkAnalysisStep
from ignite_agent.steps.known_issues import KnownIssuesStep
from ignite_agent.steps.report import FinalReportStep
from ignite_agent.steps.static_analysis import StaticAnalysisStep
from ignite_agent.steps.test_coverage import TestCoverageStep

# The active pipeline, using direct class references
PIPELINE: list[BaseStep] = [
    DocumentationStep(
        StepConfig(model_key="reasoning", max_iterations=24, max_web_calls=12)
    ),
    TestCoverageStep(
        StepConfig(
            model_key="reasoning",
            max_iterations=32,
        )
    ),
    StaticAnalysisStep(
        StepConfig(
            model_key="reasoning",
            max_iterations=64,
        )
    ),
    ForkAnalysisStep(
        StepConfig(model_key="reasoning", max_iterations=16, max_web_calls=12)
    ),
    KnownIssuesStep(
        StepConfig(model_key="reasoning", max_iterations=32, max_web_calls=16)
    ),
    AuditDeltaStep(
        StepConfig(model_key="reasoning", max_iterations=32, max_web_calls=16)
    ),
    FinalReportStep(
        StepConfig(
            model_key="reasoning",
            max_iterations=16,
        )
    ),
]
