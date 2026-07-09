from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Status = Literal["pending", "in_progress", "completed"]

_ICONS = {"pending": "○", "in_progress": "◐", "completed": "●"}


@dataclass
class PlanItem:
    content: str  # imperative form: "Run slither on the core contracts"
    active_form: str  # present-continuous form: "Running slither on the core contracts"
    status: Status = "pending"


@dataclass
class Plan:
    items: list[PlanItem] = field(default_factory=list)

    def update(self, todos: list[dict]) -> None:
        self.items = [
            PlanItem(
                content=str(t.get("content", "")).strip(),
                active_form=str(t.get("activeForm", t.get("content", ""))).strip(),
                status=t.get("status", "pending"),  # type: ignore[arg-type]
            )
            for t in todos
            if str(t.get("content", "")).strip()
        ]

    def progress(self) -> tuple[int, int]:
        done = sum(1 for i in self.items if i.status == "completed")
        return done, len(self.items)

    def as_dicts(self) -> list[dict]:
        return [
            {"content": i.content, "activeForm": i.active_form, "status": i.status}
            for i in self.items
        ]

    def render_line(self) -> str:
        if not self.items:
            return ""
        done, total = self.progress()
        active = next((i for i in self.items if i.status == "in_progress"), None)
        tail = f" — {active.active_form}" if active else ""
        return f"  plan {done}/{total}{tail}\n"

    def render_block(self) -> str:
        lines = []
        for item in self.items:
            icon = _ICONS.get(item.status, "○")
            text = item.active_form if item.status == "in_progress" else item.content
            lines.append(f"  {icon} {text}")
        return "\n".join(lines)


UPDATE_PLAN_SPEC: dict = {
    "name": "update_plan",
    "description": (
        "Maintain your own checklist for this step. Use it for any task with three or "
        "more distinct pieces of work, or where the user benefits from seeing live "
        "progress — skip it for a single trivial action.\n\n"
        "Rules:\n"
        "  - Exactly one item should be 'in_progress' at a time. Finish (or explicitly "
        "park) that item before starting the next.\n"
        "  - Mark an item 'completed' the moment it's actually done — don't batch up "
        "several finished items before reporting them.\n"
        "  - Never mark an item 'completed' if it's only partially done, if a check "
        "failed, or if you hit an unresolved error while doing it. Leave it "
        "'in_progress' (or split out a new item for the remaining work) instead.\n"
        "  - Always pass the FULL current list — this call replaces the whole "
        "checklist, it does not patch a single item.\n"
        "  - It's fine to add newly-discovered work as new pending items as you go."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "The complete, current checklist.",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "Imperative form, e.g. 'Run slither on core contracts'.",
                        },
                        "activeForm": {
                            "type": "string",
                            "description": "Present-continuous form, e.g. 'Running slither on core contracts'.",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                    },
                    "required": ["content", "activeForm", "status"],
                },
            }
        },
        "required": ["todos"],
    },
}
