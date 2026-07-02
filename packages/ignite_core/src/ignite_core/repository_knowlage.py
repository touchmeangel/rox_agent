from dataclasses import dataclass, field
from typing import Any


@dataclass
class RepositoryKnowledge:
    documentation: dict[str, Any] = field(default_factory=dict)
    architecture: dict[str, Any] = field(default_factory=dict)
    standards: list[str] = field(default_factory=list)
    static_analysis: dict[str, Any] = field(default_factory=dict)
    fork: dict[str, Any] | None = None
    previous_audits: list[dict[str, Any]] = field(default_factory=list)
    diff: dict[str, Any] | None = None

    investigation_scope: dict[str, Any] = field(
        default_factory=lambda: {
            "contracts": [],
            "entrypoints": [],
            "ignore": [],
        }
    )
