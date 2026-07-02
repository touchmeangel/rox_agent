from dataclasses import dataclass, field
from typing import Any


@dataclass
class Descriptor:
    id: str
    name: str
    granularity: str
    scenario: str
    properties: list[dict[str, Any]]
    tags: list[str] = field(default_factory=list)
    candidate_pattern: str | None = None
    exploitation_context: str | None = None
    references: list[str] = field(default_factory=list)
    severity: str | None = None
