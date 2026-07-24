from typing import Literal

from rox_core.graph import SlocKind
from rox_core.steps.base import BaseStep
from rox_core.steps.config import StepConfig


class CodebaseScalableBaseStep(BaseStep):
    def __init__(
        self,
        cfg: StepConfig,
        per_contract: float = 0.0,
        per_kloc: float = 0.0,
        min_iterations: int = 10,
    ):
        self.per_contract = per_contract
        self.per_kloc = per_kloc
        self.min_iterations = min_iterations

        super().__init__(cfg)

    @property
    def sloc_kind(self) -> SlocKind | Literal["all"]:
        return "src"

    def iteration_budget(self, context: dict) -> int:
        repository_graph = self.repo_graph

        n_contracts = len(repository_graph.contracts())
        kloc = repository_graph.sloc(kind=self.sloc_kind) / 1000

        budget = (
            self.min_iterations + n_contracts * self.per_contract + kloc * self.per_kloc
        )
        return int(min(self.cfg.max_iterations, budget))
