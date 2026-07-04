from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ignite_core.debug_logger import DebugLogger
from ignite_core.reporting import Reporter
from ignite_core.steps.base import BaseStep
from ignite_core.tools import dispatch


def _generate_repo_tree(repo_path: str) -> str:
    root = Path(repo_path)
    if not root.exists():
        return f"(Directory {repo_path} does not exist)"

    lines = []
    ignore_sets = {
        ".git",
        "node_modules",
        "lib",
        "artifacts",
        "cache",
        "out",
        "flattened",
    }

    try:
        for p in sorted(root.rglob("*")):
            if any(part in ignore_sets for part in p.parts):
                continue

            rel_parts = p.relative_to(root).parts
            depth = len(rel_parts) - 1
            indent = "  " * depth

            if p.is_dir():
                lines.append(f"{indent}📁 {p.name}/")
            else:
                lines.append(f"{indent}📄 {p.name}")
    except Exception as e:
        return f"(Error generating repository map: {str(e)})"

    if not lines:
        return "(Repository is empty or all folders were filtered out)"

    return "\n".join(lines)


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
    reporter: Reporter,
    debug_logger: DebugLogger,
    max_chars: int = 3_000,
) -> PrintResult:
    result = dispatch(
        "slither",
        {"flags": f". --print {printer} --exclude-dependencies"},
        project_path,
        reporter=reporter,
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
    ("human-summary", "Human Summary"),
    ("contract-summary", "Contract Summary"),
    ("function-summary", "Function Summary"),
    ("data-dependency", "Data Dependency"),
]


def _run_all_prints(
    project_path: str,
    reporter: Reporter,
    debug_logger: DebugLogger,
) -> list[PrintResult]:
    return [
        _slither_print(project_path, printer, label, reporter, debug_logger)
        for printer, label in _SLITHER_PRINTERS
    ]


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _run_surya_describe(
    project_path: str,
    reporter: Reporter,
    debug_logger: DebugLogger,
    max_chars: int = 4_000,
) -> str:
    result = dispatch(
        "shell",
        {
            "command": (
                f"cd '{project_path}' && "
                "find ./src -name '*.sol' | xargs surya describe 2>/dev/null "
                "|| echo 'surya unavailable or no .sol files under src/'"
            )
        },
        project_path,
        reporter=reporter,
        search_state=None,
        debug_logger=debug_logger,
    )
    output = (
        _strip_ansi((result.stdout or "").strip())
        or "(surya describe produced no output)"
    )
    if len(output) > max_chars:
        output = output[:max_chars] + "\n…(truncated)"
    return output


class DocumentationStep(BaseStep):
    name = "documentation"

    def select_context(self, global_context: dict) -> dict:
        return {}

    def findings_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "protocol_name": {
                    "type": "string",
                    "description": (
                        "The public-facing name of the protocol (e.g. 'Uniswap', 'Aave'). "
                        "Leave as an empty string if no name can be determined after checking "
                        "docs, README, package.json, and web search."
                    ),
                },
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
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "powers": {"type": "array", "items": {"type": "string"}},
                            "restrictions": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
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
                            "name": {"type": "string"},
                            "purpose": {"type": "string"},
                            "version": {"type": "string"},
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
                            "name": {"type": "string"},
                            "notes": {"type": "string"},
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
                "contract_risk_matrix": {
                    "type": "array",
                    "description": "Per-contract risk triage. One entry per in-scope contract.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "contract": {"type": "string"},
                            "sloc": {
                                "type": "integer",
                                "description": "Source lines of code (excluding blanks and comments).",
                            },
                            "external_calls": {
                                "type": "string",
                                "description": "Comma-separated list of external protocols called, or 'None'.",
                            },
                            "function_count": {"type": "integer"},
                            "holds_funds": {
                                "type": "boolean",
                                "description": "True if the contract holds ETH or ERC20 token balances.",
                            },
                            "upgradeable": {"type": "boolean"},
                            "risk_level": {
                                "type": "string",
                                "enum": ["Low", "Medium", "High", "Critical"],
                            },
                            "risk_reason": {
                                "type": "string",
                                "description": "One sentence explaining the assigned risk level.",
                            },
                        },
                        "required": [
                            "contract",
                            "sloc",
                            "external_calls",
                            "function_count",
                            "holds_funds",
                            "upgradeable",
                            "risk_level",
                            "risk_reason",
                        ],
                    },
                },
            },
            "required": [
                "protocol_summary",
                "roles",
                "external_protocols",
                "tokens",
                "solc_version",
                "architecture_notes",
                "contract_risk_matrix",
            ],
        }

    def validate_complete(self, findings: dict) -> str | None:
        if not isinstance(findings, dict):
            return "the output should be json object (python dict)"
        required = {
            "protocol_summary",
            "roles",
            "external_protocols",
            "tokens",
            "solc_version",
            "architecture_notes",
            "contract_risk_matrix",
        }
        if not required.issubset(findings.keys()):
            return f"some of the required fields are missing ({', '.join(required)})"
        if not findings.get("protocol_summary"):
            return "protocol_summary is not set which is required"
        if findings.get("solc_version", "unknown").lower() in ("unknown", "", "?"):
            return "solc_version is not set which is required"
        if (
            not isinstance(findings.get("contract_risk_matrix"), list)
            or not findings["contract_risk_matrix"]
        ):
            return "invalid contract_risk_matrix, must be a defined list"
        pname = findings.get("protocol_name")
        if pname is not None and not isinstance(pname, str):
            return "protocol_name must be a string (or omitted entirely)"
        return None

    def initial_prompt(
        self, context: dict, reporter: Reporter, debug_logger: DebugLogger
    ) -> str:
        repo_map = _generate_repo_tree(self.github_repo_path)
        surya_table = _run_surya_describe(self.project_path, reporter, debug_logger)
        prints = _run_all_prints(self.project_path, reporter, debug_logger)
        prints_section = "\n\n".join(r.render() for r in prints)

        return f"""You are running the documentation and architecture step of a security audit.

Repository Location:                      {self.github_repo_path}
Project (Tooling setup executes here):    {self.project_path}

════════════════════════════════════════
REPOSITORY STRUCTURE MAP
════════════════════════════════════════
Use this map to discover documentation files, build configs, and source directories.
You must use your tools (`read_file` or `shell`) to read files directly.

{repo_map}

════════════════════════════════════════
SURYA — FUNCTION VISIBILITY TABLE
════════════════════════════════════════
Contract-level breakdown of every function: name, visibility, mutability, and modifiers.
Use this to quickly identify privileged entry-points and state-mutating functions.

{surya_table}

════════════════════════════════════════
SLITHER ARCHITECTURE ANALYSIS  (pre-run)
════════════════════════════════════════

{prints_section}

════════════════════════════════════════
YOUR TASK
════════════════════════════════════════

You are completely responsible for exploring this repository. Analyze the Repository Map above.
Identify relevant documentation files (READMEs, specs, or docs directories), configuration settings (foundry.toml, hardhat.config.js, remappings.txt), and target source folders.

Execute `read_file` or relevant `shell` commands sequentially to read files of interest. Do not guess.

Work through the seven steps below in order.

STEP 0 — IDENTIFY THE PROTOCOL NAME  (new)
  Determine the public-facing name of this protocol. Check in this order:
    1. README.md or docs/ — look for a project title in the first heading or introduction.
    2. package.json → "name" field (strip scope prefixes like "@scope/").
    3. foundry.toml → [profile.default] name or the repo root folder name.
    4. NatSpec @title tags in the entry-point contracts.
    5. If nothing explicit is found, run `web_search` using the GitHub repo URL or a
       distinctive contract/token name to locate an official docs site or audit scope page.
  • If a name is found, set protocol_name to that string (e.g. "Morpho Blue").
  • If after all checks no name can be determined, set protocol_name to "" (empty string).
  • Do NOT guess or invent a name.

STEP 1 — UNDERSTAND THE PROTOCOL
  Locate and read the documentation files from the directory tree.
  If documentation is absent, sparse, or ambiguous:
    • Use `read_file` to scan main entry-points or look for NatSpec @notice / @dev comments.
    • Use `web_search` with the project name or GitHub URL to find external docs sites, audit scopes, or blog posts.
  Deliverable: a clear 2–4 sentence description of what the protocol does.

STEP 2 — MAP THE ROLES
  Identify every actor in the system from docs AND from access-control patterns
  in the source (modifiers, require(msg.sender == …), role constants, etc.).
  Cross-reference the Surya visibility table above to find privileged functions (onlyOwner,
  onlyRole, etc.) that confirm or extend what docs describe.
  For each role document:
    • What they can do  (powers)
    • What they are prevented from doing  (restrictions)
    • Who they are trusted by and to what degree

STEP 3 — EXTERNAL PROTOCOLS
  For each protocol: specify which interface is called, what data flows
  across that boundary, AND the version in use (check import paths,
  git modules in ./lib or foundry.toml dependencies for pinned versions).

STEP 4 — TOKENS
  List every token standard and every named token the protocol handles (ERC20, ERC721, native ETH).
  Note any token assumptions (decimals, rebase, fee-on-transfer, permit support).

STEP 5 — SOLC VERSION
  Determine the exact compiler version. Use `read_file` to check configuration files:
    1. foundry.toml  →  [profile.default] solc = "…"
    2. hardhat.config  →  solidity: {{ version: "…" }}
    3. pragma statements in the entry-point contracts
  solc_version must be a concrete string like "0.8.24", not "unknown".

STEP 6 — CONTRACT RISK MATRIX
  For every in-scope contract produce one row. Base it on everything gathered in steps 0–5
  plus the Slither function-summary and surya table above. For each contract determine:
    • sloc         — count non-blank, non-comment lines (use `shell` with wc/grep if needed)
    • external_calls — which protocols it calls into, or "None"
    • function_count — total number of functions (public + external + internal)
    • holds_funds  — does it hold ETH or ERC20 balances? (check payable functions, token mappings)
    • upgradeable  — does it use a proxy pattern or UUPSUpgradeable / TransparentUpgradeableProxy?
    • risk_level   — Low / Medium / High / Critical
    • risk_reason  — one sentence. Weight: holds_funds and external_calls push risk up;
                     pure view/library contracts push risk down.

════════════════════════════════════════
FINDINGS FORMAT  (pass to submit_findings)
════════════════════════════════════════

{{
  "protocol_name": "Morpho Blue",

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
      "name": "Uniswap",
      "version": "v3",
      "purpose": "Used to swap collateral token for USDC during liquidations via ISwapRouter"
    }}
  ],

  "tokens": [
    {{
      "standard": "ERC20",
      "name": "USDC",
      "notes": "Primary settlement token; 6 decimals assumed; permit not used"
    }}
  ],

  "solc_version": "0.8.24",

  "architecture_notes": "Describe the contract layout, inheritance hierarchy, and the primary call flows inferred from the slither output above and your source exploration.",

  "contract_risk_matrix": [
    {{
      "contract": "Vault.sol",
      "sloc": 420,
      "external_calls": "Uniswap V3, Chainlink",
      "function_count": 12,
      "holds_funds": true,
      "upgradeable": false,
      "risk_level": "High",
      "risk_reason": "Holds user funds and makes external calls to Uniswap during liquidations, creating reentrancy and price manipulation surface."
    }},
    {{
      "contract": "MathLib.sol",
      "sloc": 85,
      "external_calls": "None",
      "function_count": 6,
      "holds_funds": false,
      "upgradeable": false,
      "risk_level": "Low",
      "risk_reason": "Pure math library with no state, no external calls, and no fund custody."
    }}
  ]
}}

════════════════════════════════════════
RULES
════════════════════════════════════════

• Run STEP 0 before anything else — protocol_name must be actively searched for, not guessed.
• protocol_name may be "" if genuinely undiscoverable, but you must have checked all five
  sources listed in STEP 0 before setting it to empty.
• If documentation is missing, use web_search before guessing.
• Read source files before submitting — do not rely solely on docs.
• Every role needs at least one power.
• solc_version must be a concrete version string — "unknown" will be rejected.
• external_protocols and tokens may be empty arrays [] if truly none exist, but you must have checked the source before concluding that.
• contract_risk_matrix must have one entry per in-scope contract — do not skip contracts, even simple ones.
• Do not submit until all seven steps are complete.
"""
