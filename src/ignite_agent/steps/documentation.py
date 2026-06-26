from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ignite_agent.debug_logger import DebugLogger
from ignite_agent.rolling_window import RollingWindow
from ignite_agent.steps.base import BaseStep
from ignite_agent.tools import dispatch


_DOC_FILENAMES = [
    "README.md", "README.rst", "README.txt", "readme.md",
    "WHITEPAPER.md", "whitepaper.md",
    "ARCHITECTURE.md", "architecture.md",
    "OVERVIEW.md", "overview.md",
    "SPECIFICATION.md", "specification.md",
    "SPEC.md", "spec.md",
    "PROTOCOL.md", "protocol.md",
]

_DOC_DIRS = ["docs", "doc", "documentation", "wiki"]

_CONFIG_FILENAMES = [
    "foundry.toml",
    "remappings.txt",
]

_MAX_FILE_CHARS = 6_000
_MAX_DOC_FILES  = 10


def _read_project_docs(github_repo_path: str) -> str:
    root = Path(github_repo_path)
    collected: list[tuple[str, str]] = []

    for name in _DOC_FILENAMES:
        p = root / name
        if p.exists() and p.stat().st_size > 0:
            text = p.read_text(errors="replace")[:_MAX_FILE_CHARS]
            collected.append((name, text))

    for dir_name in _DOC_DIRS:
        d = root / dir_name
        if not d.is_dir():
            continue
        for md in sorted(d.rglob("*.md"))[:_MAX_DOC_FILES]:
            rel = str(md.relative_to(root))
            text = md.read_text(errors="replace")[:_MAX_FILE_CHARS]
            collected.append((rel, text))
        if len(collected) >= _MAX_DOC_FILES:
            break

    if not collected:
        return "(no documentation files found in repository root or docs/ directories)"

    return "\n\n---\n\n".join(f"### {path}\n\n{content}" for path, content in collected)


def _read_project_configs(github_repo_path: str) -> str:
    root = Path(github_repo_path)
    collected: list[tuple[str, str]] = []

    for name in _CONFIG_FILENAMES:
        p = root / name
        if p.exists() and p.stat().st_size > 0:
            text = p.read_text(errors="replace")[:2_000]
            collected.append((name, text))

    if not collected:
        return "(no build-config files found — solc version must be inferred from pragma statements)"

    return "\n\n".join(f"### {path}\n\n{content}" for path, content in collected)


@dataclass
class PrintResult:
    label: str
    printer: str
    output: str

    def render(self) -> str:
        header = f"════ {self.label} "
        header += "═" * max(0, 56 - len(header))
        return f"{header}\n\n{self.output}"


def _slither_print(
    project_path: str,
    printer: str,
    label: str,
    window: RollingWindow,
    debug_logger: DebugLogger,
    max_chars: int = 3_000,
) -> PrintResult:
    result = dispatch(
        "slither",
        {"flags": f". --print {printer} --exclude-dependencies"},
        project_path,
        window=window,
        search_state=None,
        debug_logger=debug_logger,
    )
    combined = "\n".join(filter(None, [result.stdout, result.stderr])).strip()

    if result.returncode == 127 or "command not found" in combined.lower():
        output = "(slither not found in PATH)"
    elif not combined:
        output = "(no output)"
    elif len(combined) > max_chars:
        output = combined[:max_chars] + "\n…(truncated)"
    else:
        output = combined

    return PrintResult(label=label, printer=printer, output=output)


_SLITHER_PRINTERS: list[tuple[str, str]] = [
    ("human-summary",    "Human Summary"),
    ("contract-summary", "Contract Summary"),
    ("inheritance-graph","Inheritance Graph"),
    ("call-graph",       "Call Graph (external chains)"),
    ("data-dependency",  "Data Dependency"),
]


def _run_all_prints(
    project_path: str,
    window: RollingWindow,
    debug_logger: DebugLogger,
) -> list[PrintResult]:
    return [
        _slither_print(project_path, printer, label, window, debug_logger)
        for printer, label in _SLITHER_PRINTERS
    ]


def _run_architecture_graph(
    project_path: str,
    window: RollingWindow,
    debug_logger: DebugLogger,
) -> str:
    result = dispatch(
        "shell",
        {
            "command": (
                f"cd '{project_path}' && "
                "surya graph ./src/**/*.sol 2>/dev/null "
                "| dot -Tpng -o architecture_graph.png "
                "&& echo 'saved: architecture_graph.png' "
                "|| echo 'surya/graphviz unavailable or no .sol files under src/'"
            )
        },
        project_path,
        window=window,
        search_state=None,
        debug_logger=debug_logger,
    )
    return (result.stdout or "").strip() or "(surya graph produced no output)"


class DocumentationStep(BaseStep):
    name = "documentation"

    def select_context(self, global_context: dict) -> dict:
        return {}

    def findings_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "protocol_summary": {
                    "type": "string",
                    "description": "2–4 sentences: what the protocol does and its core purpose.",
                },
                "roles": {
                    "type": "array",
                    "description": "Every actor in the system.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name":         {"type": "string"},
                            "description":  {"type": "string"},
                            "powers":       {"type": "array", "items": {"type": "string"}},
                            "restrictions": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["name", "description", "powers", "restrictions"],
                    },
                },
                "external_protocols": {
                    "type": "array",
                    "description": "Third-party protocols this codebase integrates with.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name":    {"type": "string"},
                            "purpose": {"type": "string"},
                        },
                        "required": ["name", "purpose"],
                    },
                },
                "tokens": {
                    "type": "array",
                    "description": "Token standards and specific tokens the protocol handles.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "standard": {"type": "string"},
                            "name":     {"type": "string"},
                            "notes":    {"type": "string"},
                        },
                        "required": ["standard", "name"],
                    },
                },
                "solc_version": {
                    "type": "string",
                    "description": "Concrete compiler version string, e.g. '0.8.24'.",
                },
                "architecture_notes": {
                    "type": "string",
                    "description": "High-level contract layout, inheritance, and key call flows.",
                },
            },
            "required": [
                "protocol_summary",
                "roles",
                "external_protocols",
                "tokens",
                "solc_version",
                "architecture_notes",
            ],
        }

    def validate_complete(self, findings: dict) -> bool:
        if not isinstance(findings, dict):
            return False
        required = {
            "protocol_summary", "roles", "external_protocols",
            "tokens", "solc_version", "architecture_notes",
        }
        if not required.issubset(findings.keys()):
            return False
        if not findings.get("protocol_summary"):
            return False
        if findings.get("solc_version", "unknown").lower() in ("unknown", "", "?"):
            return False
        return True

    def initial_prompt(
        self, context: dict, window: RollingWindow, debug_logger: DebugLogger
    ) -> str:
        docs          = _read_project_docs(self.github_repo_path)
        configs       = _read_project_configs(self.github_repo_path)
        graph_msg     = _run_architecture_graph(self.project_path, window, debug_logger)
        prints        = _run_all_prints(self.project_path, window, debug_logger)
        prints_section = "\n\n".join(r.render() for r in prints)

        return f"""You are running the documentation and architecture step of a security audit.

Repository (docs): {self.github_repo_path}
Project (foundry setup, tooling executes here):   {self.project_path}
Architecture graph:         {graph_msg}

════════════════════════════════════════
PROJECT DOCUMENTATION
════════════════════════════════════════

{docs}

════════════════════════════════════════
BUILD CONFIGURATION  (solc version, remappings…)
════════════════════════════════════════

{configs}

════════════════════════════════════════
SLITHER ARCHITECTURE ANALYSIS  (pre-run)
════════════════════════════════════════

{prints_section}

════════════════════════════════════════
YOUR TASK
════════════════════════════════════════

Work through the five steps below in order. Each step has a concrete
deliverable — do not skip one because the docs look thin; use tools to fill gaps.

STEP 1 — UNDERSTAND THE PROTOCOL
  Read the documentation above carefully.
  If it is absent, sparse, or ambiguous:
    • Use read_file to scan README, natspec comments in key contracts, and
      any NatSpec @notice / @dev comments on public functions.
    • Use web_search with the project name or GitHub URL to find the team's
      docs site, audit scope, or announcement post.
  Deliverable: a clear 2–4 sentence description of what the protocol does.

STEP 2 — MAP THE ROLES
  Identify every actor in the system from docs AND from access-control patterns
  in the source (modifiers, require(msg.sender == …), role constants, etc.).
  Common actors: Buyer, Seller, Arbiter, Owner, Admin, Relayer, Keeper, Liquidator…
  For each role document:
    • What they can do  (powers)
    • What they are prevented from doing  (restrictions)
    • Who they are trusted by and to what degree

STEP 3 — EXTERNAL PROTOCOLS
  List every third-party protocol this codebase calls into:
    DEXes (Uniswap, Curve, Balancer…)
    Lending (Morpho, Aave, Compound…)
    Oracles (Chainlink, Pyth, Tellor…)
    Bridges, vaults, yield aggregators…
  For each: which interface is called and what value/data flows across that boundary.

STEP 4 — TOKENS
  List every token standard and every named token the protocol handles:
    Standards: ERC20, ERC721, ERC1155, native ETH / WETH…
    Named tokens: USDC, WETH, DAI, protocol-specific tokens…
  Note any token assumptions (decimals, rebase, fee-on-transfer, permit support).

STEP 5 — SOLC VERSION
  Determine the exact compiler version. Check in this order:
    1. foundry.toml  →  [profile.default] solc = "…"
    2. hardhat.config  →  solidity: {{ version: "…" }}
    3. pragma statements in the entry-point contracts
  solc_version must be a concrete string like "0.8.24", not "unknown".

════════════════════════════════════════
FINDINGS FORMAT  (pass to submit_findings)
════════════════════════════════════════

{{
  "protocol_summary": "2–4 sentences describing what the protocol does.",

  "roles": [
    {{
      "name": "Buyer",
      "description": "Initiates purchases and locks funds in escrow.",
      "powers": [
        "Can release funds to the seller after receiving goods",
        "Can open a dispute within the challenge window"
      ],
      "restrictions": [
        "Cannot withdraw funds unilaterally before the timeout expires",
        "Cannot act as arbiter in their own trade"
      ]
    }}
  ],

  "external_protocols": [
    {{
      "name": "Uniswap V3",
      "purpose": "Used to swap collateral token for USDC during liquidations via ISwapRouter"
    }}
  ],

  "tokens": [
    {{
      "standard": "ERC20",
      "name": "USDC",
      "notes": "Primary settlement token; 6 decimals assumed; permit not used"
    }},
    {{
      "standard": "native ETH",
      "name": "ETH",
      "notes": "Accepted as collateral; wrapped to WETH internally"
    }}
  ],

  "solc_version": "0.8.24",

  "architecture_notes": "Describe the contract layout, inheritance hierarchy, and the primary call flows inferred from the slither output above."
}}

════════════════════════════════════════
RULES
════════════════════════════════════════

• If documentation is missing, use web_search before guessing.
• Read source files before submitting — do not rely solely on docs.
• Every role needs at least one power.
• solc_version must be a concrete version string — "unknown" will be rejected.
• external_protocols and tokens may be empty arrays [] if truly none exist,
  but you must have checked the source before concluding that.
• Do not submit until all five steps are complete.
"""