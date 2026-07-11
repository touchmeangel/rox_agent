import inspect
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal
from pathlib import Path

from slither.detectors import all_detectors
from slither.detectors.abstract_detector import AbstractDetector
from slither.slither import Slither

from ignite_core.foundry_config import FoundryConfig, load_foundry_config

_GRAPH_CACHE: dict[str, "Graph"] = {}

_EXCLUDED_CHECKS = frozenset(
    {
        "naming-convention",
        "solc-version",
        "low-level-calls",
        "pragma",
        "similar-names",
    }
)

SlocKind = Literal["src", "test", "script", "lib", "other"]

def get_or_build_graph(path: str) -> "Graph | None":
    if not path:
        return None
    if path not in _GRAPH_CACHE:
        _GRAPH_CACHE[path] = build_graph(path)
    return _GRAPH_CACHE[path]


@dataclass
class Graph:
    slither: Slither = field()
    foundry_config: FoundryConfig = field()
    node_kind: dict[str, str] = field(default_factory=dict)
    node_file: dict[str, str] = field(default_factory=dict)
    node_meta: dict[str, dict] = field(default_factory=dict)
    out_edges: dict[str, list[tuple[str, str]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    in_edges: dict[str, list[tuple[str, str]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    detectors: list[dict] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

    def add_node(self, node_id, kind, file="", **meta):
        self.node_kind[node_id] = kind
        self.node_file[node_id] = file
        self.node_meta[node_id] = meta

    def add_edge(self, src, dst, kind):
        self.out_edges[src].append((dst, kind))
        self.in_edges[dst].append((src, kind))

    def callees(self, node):
        return [
            d
            for d, k in self.out_edges.get(node, [])
            if k in ("calls", "calls_external")
        ]

    def callers(self, node):
        return [
            s
            for s, k in self.in_edges.get(node, [])
            if k in ("calls", "calls_external")
        ]

    def inherits(self, node):
        return [d for d, k in self.out_edges.get(node, []) if k == "inherits"]

    def inherited_by(self, node):
        return [s for s, k in self.in_edges.get(node, []) if k == "inherits"]

    def impact(self, node, depth=3):
        seen = {node}
        frontier = {node}
        for _ in range(depth):
            frontier = {s for n in frontier for s in self.callers(n)} - seen
            if not frontier:
                break
            seen |= frontier
        return seen - {node}

    def dead_code(self):
        called = {
            d for edges in self.out_edges.values() for d, k in edges if k == "calls"
        }
        return sorted(
            n
            for n, kind in self.node_kind.items()
            if kind == "function" and n not in called
        )

    def search(self, pattern):
        pattern = pattern.lower()
        return sorted(n for n in self.node_kind if pattern in n.lower())

    def contracts(self) -> list[str]:
        return sorted(n for n, k in self.node_kind.items() if k == "contract")

    def functions(self, contract: str | None = None) -> list[str]:
        return sorted(
            n
            for n, k in self.node_kind.items()
            if k == "function" and (contract is None or n.startswith(f"{contract}."))
        )

    def state_vars(self, contract: str | None = None) -> list[str]:
        return sorted(
            n
            for n, k in self.node_kind.items()
            if k == "state_var" and (contract is None or n.startswith(f"{contract}."))
        )
        
    def sloc(self, kind: SlocKind = "src") -> int:
        all_lines: set[tuple[str, int]] = set()
        for c in self.slither.contracts:
            file = str(c.source_mapping.filename.relative)
            if kind != "all" and _dir_of(file, self.foundry_config) != kind:
                continue
            all_lines.update((file, ln) for ln in c.source_mapping.lines)
        return len(all_lines)
    
    def test_sloc(self) -> int:
        return self.sloc(kind="test")

def _dir_of(file_rel: str, cfg: FoundryConfig) -> SlocKind:
    parts = Path(file_rel).parts
    if not parts:
        return "other"
    top = parts[0]
    if top == cfg.test:
        return "test"
    if top == cfg.script:
        return "script"
    if top in cfg.libs:
        return "lib"
    if top == cfg.src:
        return "src"
    return "other" 

def run_detectors(sl: Slither) -> list[dict]:
    for name in dir(all_detectors):
        cls = getattr(all_detectors, name)
        if (
            inspect.isclass(cls)
            and issubclass(cls, AbstractDetector)
            and cls is not AbstractDetector
        ):
            if getattr(cls, "ARGUMENT", None) not in _EXCLUDED_CHECKS:
                sl.register_detector(cls)
    grouped = sl.run_detectors()
    return [item for group in grouped for item in (group or [])]


def build_graph(project_path: str) -> Graph:
    cfg = load_foundry_config(project_path)
    sl = Slither(project_path, foundry_out_directory="out")

    g = Graph(slither=sl, foundry_config=cfg)
    g.files = sorted({str(c.source_mapping.filename.relative) for c in sl.contracts})

    for c in sl.contracts:
        file = str(c.source_mapping.filename.relative)
        g.add_node(
            c.name,
            "contract",
            file=file,
            is_interface=c.is_interface,
            is_abstract=c.is_abstract,
        )
        for parent in c.inheritance:
            g.add_edge(c.name, parent.name, "inherits")

        for var in c.state_variables:
            vnode = f"{c.name}.{var.name}"
            g.add_node(vnode, "state_var", type=str(var.type))
            g.add_edge(c.name, vnode, "declares")

        for fn in c.functions:
            fnode = f"{c.name}.{fn.name}"
            g.add_node(
                fnode,
                "function",
                file=file,
                visibility=fn.visibility,
                modifiers=[m.name for m in fn.modifiers],
            )
            g.add_edge(c.name, fnode, "declares")
            for var in fn.state_variables_written:
                g.add_edge(fnode, f"{var.contract.name}.{var.name}", "writes")
            for var in fn.state_variables_read:
                g.add_edge(fnode, f"{var.contract.name}.{var.name}", "reads")
            for callee in fn.internal_calls:
                if hasattr(callee, "name"):
                    g.add_edge(fnode, f"{c.name}.{callee.name}", "calls")
            for tgt_c, tgt_fn in fn.high_level_calls:
                g.add_edge(fnode, f"{tgt_c.name}.{tgt_fn.name}", "calls_external")

    g.detectors = run_detectors(sl)

    return g
