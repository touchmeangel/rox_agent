from dataclasses import dataclass, field
from typing import Any

@dataclass
class InvestigationScope:
    contracts: list[str] = field(default_factory=list)
    entrypoints: list[str] = field(default_factory=list)
    ignore: list[str] = field(default_factory=list)

@dataclass
class RepositoryKnowledge:
    semantic_tags: list[str] = field(default_factory=list)
    semantic_evidence: dict[str, list[str]] = field(default_factory=dict)
    detected_protocols: list[str] = field(default_factory=list)
    documentation: dict[str, Any] = field(default_factory=dict)
    static_analysis: dict[str, Any] = field(default_factory=dict)
    previous_audits: list[dict[str, Any]] = field(default_factory=list)
    diff: dict[str, Any] | None = None
    fork: dict[str, Any] | None = None
    investigation_scope: InvestigationScope = field(
        default_factory=InvestigationScope
    )