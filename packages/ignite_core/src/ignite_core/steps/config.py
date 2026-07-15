from dataclasses import dataclass, field
from typing import Literal

from ignite_core.graph import SlocKind
from ignite_core.model_config import ModelConfig
from ignite_core.graph import Graph

@dataclass
class StepConfig:
    max_iterations: int = field()
    models: list[ModelConfig] = field(default_factory=list)
    max_web_calls: int | None = 6
    scales_with_code: bool = False
    sloc_kind: SlocKind | Literal["all"] = "src"
    per_contract: float = 0.0
    per_kloc: float = 0.0
    min_iterations: int = 10


    def compute_iteration_budget(self, repository_graph: Graph) -> int:
        if not self.scales_with_code:
            return self.max_iterations

        n_contracts = len(repository_graph.contracts())
        kloc = repository_graph.sloc(kind=self.sloc_kind) / 1000

        budget = (
            self.min_iterations
            + n_contracts * self.per_contract
            + kloc * self.per_kloc
        )
        return int(min(self.max_iterations, budget))