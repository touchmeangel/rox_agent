from ignite_discovery.config import MODELS

from ignite_core.steps.base import BaseStep
from ignite_core.steps.config import StepConfig
from ignite_discovery.steps.audit_delta import AuditDeltaStep
from ignite_discovery.steps.documentation import DocumentationStep
from ignite_discovery.steps.fork_analysis import ForkAnalysisStep
from ignite_discovery.steps.invariant_testing import InvariantTestingStep
from ignite_discovery.steps.known_issues import KnownIssuesStep
from ignite_discovery.steps.report import FinalReportStep
from ignite_discovery.steps.static_analysis import StaticAnalysisStep
from ignite_discovery.steps.test_coverage import TestCoverageStep

# The active pipeline, using direct class references
PIPELINE: list[BaseStep] = [
    DocumentationStep(
        StepConfig(models=MODELS["reasoning"], max_iterations=32, max_web_calls=12)
    ),
    TestCoverageStep(
        StepConfig(
            models=MODELS["reasoning"],
            max_iterations=64,
        )
    ),
    StaticAnalysisStep(
        StepConfig(
            models=MODELS["reasoning"],
            max_iterations=64,
        )
    ),
    ForkAnalysisStep(
        StepConfig(models=MODELS["reasoning"], max_iterations=16, max_web_calls=12)
    ),
    KnownIssuesStep(
        StepConfig(models=MODELS["reasoning"], max_iterations=32, max_web_calls=16)
    ),
    AuditDeltaStep(
        StepConfig(models=MODELS["reasoning"], max_iterations=32, max_web_calls=16)
    ),
    InvariantTestingStep(
        StepConfig(
            models=MODELS["reasoning"],
            max_iterations=64,
            max_web_calls=12
        )
    ),
    FinalReportStep(
        StepConfig(
            models=MODELS["reasoning"],
            max_iterations=16,
        )
    ),
]
