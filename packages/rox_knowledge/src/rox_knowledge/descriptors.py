from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from rox_knowledge.descriptor import Descriptor
from rox_knowledge.protocol_tags import is_valid_protocol_tag
from rox_knowledge.tags import is_valid_tag

_DESCRIPTORS_DIR = Path(__file__).parent / "data" / "descriptors"

_VALID_GRANULARITIES = ("contract", "function", "location", "integration")
_REQUIRED_PROPERTY_FIELDS = ("id", "check")


@lru_cache(maxsize=1)
def load_descriptors() -> list[Descriptor]:
    descriptors: list[Descriptor] = []
    seen_ids: set[str] = set()

    for path in sorted(_DESCRIPTORS_DIR.rglob("*.yaml")):
        raw = yaml.safe_load(path.read_text())

        descriptor_id = raw["id"]
        if descriptor_id in seen_ids:
            raise ValueError(f"Duplicate descriptor id {descriptor_id!r} in {path}")
        seen_ids.add(descriptor_id)

        if not raw.get("name", "").strip():
            raise ValueError(
                f"Descriptor {descriptor_id!r} ({path}) has an empty name."
            )

        if not raw.get("scenario", "").strip():
            raise ValueError(
                f"Descriptor {descriptor_id!r} ({path}) has an empty scenario."
            )

        tags = raw.get("tags", [])
        for tag in tags:
            if not is_valid_tag(tag) and not is_valid_protocol_tag(tag):
                raise ValueError(
                    f"Descriptor {descriptor_id!r} ({path}) references unknown tag "
                    f"{tag!r} — add it to tags.yaml or protocol_tags.yaml first."
                )

        granularity = raw.get("granularity", "contract")
        if granularity not in _VALID_GRANULARITIES:
            raise ValueError(
                f"Descriptor {descriptor_id!r} ({path}) has invalid granularity "
                f"{granularity!r} — must be one of {_VALID_GRANULARITIES}."
            )

        if granularity == "location" and not raw.get("candidate_pattern"):
            raise ValueError(
                f"Descriptor {descriptor_id!r} ({path}) has granularity='location' "
                "but no candidate_pattern — required to enumerate candidates."
            )

        properties = raw.get("properties", [])
        if not properties:
            raise ValueError(
                f"Descriptor {descriptor_id!r} ({path}) has no properties — every "
                "descriptor needs at least one checkable property."
            )

        for prop in properties:
            if not isinstance(prop, dict):
                raise ValueError(
                    f"Descriptor {descriptor_id!r} ({path}) has a property that "
                    f"isn't a mapping: {prop!r}. This usually means a "
                    "check/confirm_with/severity block dedented out from under "
                    "its containing '- id: ...' list item and YAML parsed it as "
                    "something else entirely — check indentation."
                )
            missing = [f for f in _REQUIRED_PROPERTY_FIELDS if not prop.get(f)]
            if missing:
                raise ValueError(
                    f"Descriptor {descriptor_id!r} ({path}) property "
                    f"{prop.get('id', '?')!r} is missing required field(s): "
                    f"{', '.join(missing)}. If this field exists in the source "
                    "YAML but isn't showing up here, check indentation — a "
                    "dedented check/confirm_with/severity block becomes a "
                    "sibling of 'properties:' instead of a child of this list "
                    "item, and silently disappears from this property's dict."
                )

        descriptors.append(
            Descriptor(
                id=descriptor_id,
                name=raw["name"],
                scenario=raw["scenario"].strip(),
                properties=properties,
                tags=tags,
                granularity=granularity,
                candidate_pattern=raw.get("candidate_pattern"),
                exploitation_context=raw.get("exploitation_context"),
                references=raw.get("references", []),
                severity=raw.get("severity"),
            )
        )

    return descriptors


def get_descriptor(descriptor_id: str) -> Descriptor:
    for d in load_descriptors():
        if d.id == descriptor_id:
            return d
    raise KeyError(f"Unknown descriptor id: {descriptor_id!r}")
