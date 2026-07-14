import inspect
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ignite_core.foundry_config import FoundryConfig, load_foundry_config
from slither.detectors import all_detectors
from slither.detectors.abstract_detector import AbstractDetector
from slither.slither import Slither
from slither.core.declarations.function import Function
from slither.core.variables.state_variable import StateVariable
from slither.slithir.operations import LibraryCall
from slither.core.declarations.solidity_variables import SolidityFunction
from slither.slithir.operations import InternalCall

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


def _target_name(tgt) -> str | None:
    if isinstance(tgt, (Function, StateVariable)):
        return tgt.name
    if isinstance(tgt, LibraryCall):
        if tgt.function is not None:
            return tgt.function.name
        return str(tgt.function_name)
    return None


def _target_label(src) -> str | None:
    if src is None:
        return None
    return getattr(src, "name", None) or str(src)

def _callee_name(callee) -> str | None:
    if isinstance(callee, (Function, SolidityFunction)):
        return callee.name
    if isinstance(callee, InternalCall):
        if callee.function is not None:
            return callee.function.name
        return str(callee.function_name)
    return None

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

    def sloc(self, kind: SlocKind | Literal["all"] = "src") -> int:
        all_lines: set[tuple[str, int]] = set()
        for c in self.slither.contracts:
            file = str(c.source_mapping.filename.relative)
            if kind != "all" and _dir_of(file, self.foundry_config) != kind:
                continue
            all_lines.update((file, ln) for ln in c.source_mapping.lines)
        return len(all_lines)


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
                name = _callee_name(callee)
                if name:
                    g.add_edge(fnode, f"{c.name}.{name}", "calls")
            for tgt_c, tgt_fn in fn.high_level_calls:
                c_name = _target_label(tgt_c)
                f_name = _target_name(tgt_fn)
                if c_name and f_name:
                    g.add_edge(fnode, f"{c_name}.{f_name}", "calls_external")

    g.detectors = run_detectors(sl)

    return g
