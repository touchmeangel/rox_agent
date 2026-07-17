from dataclasses import asdict, dataclass, field

from ignite_core.repository_knowledge import RepositoryKnowledge
from ignite_knowledge.descriptor import Descriptor


@dataclass
class MissionLeg:
    contract: str
    vulnerability: str
    descriptor: Descriptor
    knowledge: RepositoryKnowledge
    hints: list[str] = field(default_factory=list)


@dataclass
class Mission:
    id: str
    legs: list[MissionLeg]
    planner_rationale: str = ""
    shared_hints: list[str] = field(default_factory=list)

    @property
    def is_bundled(self) -> bool:
        return len(self.legs) > 1

    @property
    def vulnerability(self) -> str:
        if not self.is_bundled:
            return self.legs[0].vulnerability
        return " -> ".join(leg.vulnerability for leg in self.legs)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "vulnerability": self.vulnerability,
            "planner_rationale": self.planner_rationale,
            "shared_hints": self.shared_hints,
            "legs": [
                {
                    "contract": leg.contract,
                    "vulnerability": leg.vulnerability,
                    "descriptor": asdict(leg.descriptor),
                    "knowledge": asdict(leg.knowledge),
                    "hints": leg.hints,
                }
                for leg in self.legs
            ],
        }
