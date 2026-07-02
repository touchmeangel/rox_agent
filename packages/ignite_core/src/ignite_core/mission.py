from dataclasses import dataclass, field
from ignite_core.repository_knowlage import RepositoryKnowledge
from packages.ignite_knowledge.src.ignite_knowledge.descriptor import Descriptor


@dataclass
class Mission:
    id: str

    contract: str

    vulnerability: str

    descriptor: Descriptor

    knowledge: RepositoryKnowledge = field(
        default_factory=RepositoryKnowledge
    )

    hints: list[str] = field(default_factory=list)