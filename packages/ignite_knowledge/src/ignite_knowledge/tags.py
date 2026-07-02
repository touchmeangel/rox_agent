from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

_DATA_DIR = Path(__file__).parent / "data"
_TAGS_FILE = _DATA_DIR / "tags.yaml"


@dataclass(frozen=True)
class TagDefinition:
    id: str
    description: str


@lru_cache(maxsize=1)
def load_tag_registry() -> dict[str, TagDefinition]:
    raw = yaml.safe_load(_TAGS_FILE.read_text())
    registry: dict[str, TagDefinition] = {}
    for entry in raw.get("tags", []):
        tag = TagDefinition(id=entry["id"], description=entry["description"].strip())
        if tag.id in registry:
            raise ValueError(f"Duplicate tag id in tags.yaml: {tag.id!r}")
        registry[tag.id] = tag
    return registry


def all_tag_ids() -> list[str]:
    return sorted(load_tag_registry().keys())


def is_valid_tag(tag_id: str) -> bool:
    return tag_id in load_tag_registry()
