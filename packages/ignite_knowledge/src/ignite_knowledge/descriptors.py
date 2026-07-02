from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from ignite_knowledge.descriptor import Descriptor
from ignite_knowledge.tags import is_valid_tag

_DESCRIPTORS_DIR = Path(__file__).parent / "data" / "descriptors"


@lru_cache(maxsize=1)
def load_descriptors() -> list[Descriptor]:
    descriptors: list[Descriptor] = []
    seen_ids: set[str] = set()

    for path in sorted(_DESCRIPTORS_DIR.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text())

        descriptor_id = raw["id"]
        if descriptor_id in seen_ids:
            raise ValueError(f"Duplicate descriptor id {descriptor_id!r} in {path}")
        seen_ids.add(descriptor_id)

        tags = raw.get("tags", [])
        for tag in tags:
            if not is_valid_tag(tag):
                raise ValueError(
                    f"Descriptor {descriptor_id!r} ({path}) references unknown tag "
                    f"{tag!r} — add it to tags.yaml first."
                )

        granularity = raw.get("granularity", "contract")
        if granularity not in ("contract", "location"):
            raise ValueError(
                f"Descriptor {descriptor_id!r} ({path}) has invalid granularity "
                f"{granularity!r} — must be 'contract' or 'location'."
            )
        if granularity == "location" and not raw.get("candidate_pattern"):
            raise ValueError(
                f"Descriptor {descriptor_id!r} ({path}) has granularity='location' "
                "but no candidate_pattern — required to enumerate candidates."
            )

        descriptors.append(
            Descriptor(
                id=descriptor_id,
                name=raw["name"],
                scenario=raw["scenario"].strip(),
                properties=raw.get("properties", []),
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