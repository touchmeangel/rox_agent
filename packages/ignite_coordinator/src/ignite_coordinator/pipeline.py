from ignite_coordinator.config import MODELS
from ignite_coordinator.steps.audit_delta import AuditDeltaStep
from ignite_coordinator.steps.documentation import DocumentationStep
from ignite_coordinator.steps.fork_analysis import ForkAnalysisStep

# from ignite_coordinator.steps.invariant_testing import InvariantTestingStep
from ignite_coordinator.steps.known_issues import KnownIssuesStep
from ignite_coordinator.steps.planning import PlanningStep
from ignite_coordinator.steps.semantic_tagging import SemanticTaggingStep
from ignite_coordinator.steps.static_analysis import StaticAnalysisStep
from ignite_coordinator.steps.test_coverage import TestCoverageStep
from ignite_core.steps.base import BaseStep
from ignite_core.steps.config import StepConfig

# The active pipeline, using direct class references
PIPELINE: list[BaseStep] = [
    DocumentationStep(
        StepConfig(models=MODELS["reasoning"], max_iterations=64, max_web_calls=12)
    ),
    TestCoverageStep(
        StepConfig(
            models=MODELS["reasoning"],
            max_iterations=48,
        )
    ),
    StaticAnalysisStep(
        StepConfig(
            models=MODELS["reasoning"],
            max_iterations=64,
        )
    ),
    ForkAnalysisStep(
        StepConfig(models=MODELS["reasoning"], max_iterations=32, max_web_calls=12)
    ),
    AuditDeltaStep(
        StepConfig(models=MODELS["reasoning"], max_iterations=48, max_web_calls=16)
    ),
    # InvariantTestingStep(
    #     StepConfig(
    #         models=MODELS["reasoning"], max_iterations=64, max_web_calls=12
    #     )
    # ), # Needs to be moved to POC step
    SemanticTaggingStep(
        StepConfig(models=MODELS["reasoning"], max_iterations=48, max_web_calls=12)
    ),
    KnownIssuesStep(
        StepConfig(models=MODELS["reasoning"], max_iterations=48, max_web_calls=16)
    ),
    PlanningStep(
        StepConfig(
            models=MODELS["reasoning"],
            max_iterations=64,
        )
    ),
]
