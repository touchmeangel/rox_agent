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

SCALABLE_ITERATIONS_MAX_LIMIT = 128
SCALABLE_ITERATIONS_MIN_LIMIT = 24

# The active pipeline, using direct class references
PIPELINE: list[BaseStep] = [
    DocumentationStep(
        StepConfig(
            models=MODELS["reasoning"],
            max_iterations=SCALABLE_ITERATIONS_MAX_LIMIT,
            min_iterations=SCALABLE_ITERATIONS_MIN_LIMIT,
            max_web_calls=12,
            scales_with_code=True,
            per_contract=0.3,
            per_kloc=1.5,
        )
    ),
    TestCoverageStep(
        StepConfig(
            models=MODELS["reasoning"],
            max_iterations=SCALABLE_ITERATIONS_MAX_LIMIT,
            min_iterations=SCALABLE_ITERATIONS_MIN_LIMIT,
            scales_with_code=True,
            sloc_kind="test",
            per_contract=0.3,
            per_kloc=1.0,
        )
    ),
    StaticAnalysisStep(
        StepConfig(
            models=MODELS["reasoning"],
            max_iterations=SCALABLE_ITERATIONS_MAX_LIMIT,
            min_iterations=SCALABLE_ITERATIONS_MIN_LIMIT,
            scales_with_code=True,
            per_contract=0.4,
            per_kloc=0.5,
        )
    ),
    ForkAnalysisStep(
        StepConfig(models=MODELS["reasoning"], max_iterations=64, max_web_calls=64)
    ),
    AuditDeltaStep(
        StepConfig(models=MODELS["reasoning"], max_iterations=64, max_web_calls=64)
    ),
    # InvariantTestingStep(
    #     StepConfig(
    #         models=MODELS["reasoning"], max_iterations=64, max_web_calls=12
    #     )
    # ), # Needs to be moved to POC step
    SemanticTaggingStep(
        StepConfig(
            models=MODELS["reasoning"],
            max_iterations=SCALABLE_ITERATIONS_MAX_LIMIT,
            min_iterations=SCALABLE_ITERATIONS_MIN_LIMIT,
            scales_with_code=True,
            per_contract=1.0,
            per_kloc=0.3,
            max_web_calls=12,
        )
    ),
    KnownIssuesStep(
        StepConfig(models=MODELS["reasoning"], max_iterations=64, max_web_calls=32)
    ),  # depends on amount of protocols
    PlanningStep(
        StepConfig(
            models=MODELS["reasoning"],
            max_iterations=64,
        )
    ),  # depends on amount appliable descriptors
]
