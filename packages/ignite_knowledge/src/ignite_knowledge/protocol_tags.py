from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

_DATA_DIR = Path(__file__).parent / "data"
_PROTOCOL_TAGS_FILE = _DATA_DIR / "protocol_tags.yaml"


@dataclass(frozen=True)
class ProtocolTagDefinition:
    id: str
    description: str


@lru_cache(maxsize=1)
def load_protocol_tag_registry() -> dict[str, ProtocolTagDefinition]:
    raw = yaml.safe_load(_PROTOCOL_TAGS_FILE.read_text())
    registry: dict[str, ProtocolTagDefinition] = {}
    for entry in raw.get("protocol_tags", []):
        tag = ProtocolTagDefinition(
            id=entry["id"], description=entry["description"].strip()
        )
        if tag.id in registry:
            raise ValueError(
                f"Duplicate protocol tag id in protocol_tags.yaml: {tag.id!r}"
            )
        registry[tag.id] = tag
    return registry


def all_protocol_tag_ids() -> list[str]:
    return sorted(load_protocol_tag_registry().keys())


def is_valid_protocol_tag(tag_id: str) -> bool:
    return tag_id in load_protocol_tag_registry()


def match_protocol_tags(name: str) -> list[str]:
    normalized = "".join(ch for ch in name.lower() if ch.isalnum())
    if not normalized:
        return []
    return [
        tag_id
        for tag_id in load_protocol_tag_registry()
        if tag_id in normalized or normalized in tag_id
    ]
