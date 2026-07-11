from __future__ import annotations

from pathlib import Path

from ignite_core.debug_logger import DebugLogger
from ignite_core.reporting import Reporter
from ignite_core.steps.base import BaseStep


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


def _fmt_architecture(graph) -> str:
    if graph is None:
        return "(Repo graph unavailable — compilation failed. Read files directly with read_file/shell instead.)"
    lines = []
    for contract in graph.contracts():
        meta = graph.node_meta.get(contract, {})
        kind = "interface" if meta.get("is_interface") else "abstract" if meta.get("is_abstract") else "contract"
        parents = graph.inherits(contract)
        lines.append(f"\n### {contract} [{kind}]" + (f" (inherits: {', '.join(parents)})" if parents else ""))

        svars = graph.state_vars(contract)
        if svars:
            lines.append("  State: " + ", ".join(f"{v.split('.',1)[1]}: {graph.node_meta[v]['type']}" for v in svars))

        for f in graph.functions(contract):
            fmeta = graph.node_meta.get(f, {})
            writes = sorted({d.split(".",1)[1] for d, k in graph.out_edges.get(f, []) if k == "writes"})
            reads  = sorted({d.split(".",1)[1] for d, k in graph.out_edges.get(f, []) if k == "reads"})
            calls  = sorted({d for d, k in graph.out_edges.get(f, []) if k in ("calls", "calls_external")})
            mods = ", ".join(fmeta.get("modifiers", [])) or "none"
            lines.append(f"  - {f.split('.',1)[1]}({fmeta.get('visibility','?')}, modifiers: {mods})")
            if writes: lines.append(f"      writes: {', '.join(writes)}")
            if reads:  lines.append(f"      reads: {', '.join(reads)}")
            if calls:  lines.append(f"      calls: {', '.join(calls)}")
    return "\n".join(lines) if lines else "(no contracts parsed)"


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
                "tokens",
                "solc_version",
                "architecture_notes",
                "contract_risk_matrix",
            ],
        }

    def validate_complete(self, findings: dict) -> str | None:
        if not isinstance(findings, dict):
            return "findings must be json object (python dict)"

        required = {
            "protocol_summary",
            "roles",
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
        architecture_section = _fmt_architecture(self.repo_graph)

        return f"""<audit_step name="documentation">

<scope>
You are running the documentation and architecture step of a security audit. This step
builds the map every later step and worker mission relies on: what the protocol does, who
the actors are, and a first-pass risk triage per contract. It does not confirm
vulnerabilities or produce proof-of-concept code — that happens later, per-candidate,
in the worker phase.
</scope>

<repository>
<repo_location>{self.github_repo_path}</repo_location>
<project_path note="tooling executes here">{self.project_path}</project_path>

<repo_map note="use this to find documentation, build configs, and source directories; you must still read_file/shell into anything of interest">
{repo_map}
</repo_map>
</repository>

<pre_scan>
<repo_graph note="parsed directly from the compiled contracts — every contract, its inheritance, state variables, and every function with its visibility, modifiers, and what it reads/writes/calls. This is ground truth from compilation, not a heuristic guess, but it only covers Solidity structure: it has no docs, no NatSpec, no protocol names, and 'calls'/'writes' are direct edges only, not full taint-tracked data flow.">
{architecture_section}
</repo_graph>
</pre_scan>

<task>
You are completely responsible for exploring this repository. Analyze the repository map
and repo graph above. Identify relevant documentation files (READMEs, specs, or docs
directories), configuration settings (foundry.toml, hardhat.config.js, remappings.txt), and
target source folders. Execute read_file or relevant shell commands sequentially to read
files of interest. Do not guess. Work through the steps below in order.

<step id="0" title="Identify the protocol name">
Determine the public-facing name of this protocol. Check in this order:
  1. README.md or docs/ — look for a project title in the first heading or introduction.
  2. package.json → "name" field (strip scope prefixes like "@scope/").
  3. foundry.toml → [profile.default] name or the repo root folder name.
  4. NatSpec @title tags in the entry-point contracts.
  5. If nothing explicit is found, run web_search using the GitHub repo URL or a
     distinctive contract/token name to locate an official docs site or audit scope page.
If a name is found, set protocol_name to that string (e.g. "Morpho Blue"). If after all
checks no name can be determined, set protocol_name to "" (empty string). Do NOT guess or
invent a name.
</step>

<step id="1" title="Understand the protocol">
Locate and read the documentation files from the directory tree. If documentation is
absent, sparse, or ambiguous:
  - Use read_file to scan main entry-points or look for NatSpec @notice / @dev comments.
  - Use web_search with the project name or GitHub URL to find external docs sites, audit
    scopes, or blog posts.
Deliverable: a clear 2-4 sentence description of what the protocol does.
</step>

<step id="2" title="Map the roles">
Identify every actor in the system from docs AND from access-control patterns in the source
(modifiers, require(msg.sender == ...), role constants, etc.). The repo graph above already
lists each function's visibility and modifiers — scan it first to find privileged functions
(onlyOwner, onlyRole, etc.) before opening files, then read_file to confirm what each
modifier actually enforces. For each role document:
  - What they can do (powers)
  - What they are prevented from doing (restrictions)
  - Who they are trusted by and to what degree
</step>

<step id="3" title="Tokens">
List every token standard and every named token the protocol handles (ERC20, ERC721, native
ETH). Note any token assumptions (decimals, rebase, fee-on-transfer, permit support).
</step>

<step id="4" title="Solc version">
Determine the exact compiler version. Use read_file to check configuration files:
  1. foundry.toml → [profile.default] solc = "..."
  2. hardhat.config → solidity: {{ version: "..." }}
  3. pragma statements in the entry-point contracts
solc_version must be a concrete string like "0.8.24", not "unknown".
</step>

<step id="5" title="Contract risk matrix">
For every in-scope contract produce one row. The repo graph above already gives you, per
contract, its full function list with reads/writes/calls — use it as your starting point
instead of re-deriving structure by hand, then confirm anything ambiguous with read_file.
This is a triage/priority signal for downstream steps — not a vulnerability finding. For
each contract determine:
  - sloc — count non-blank, non-comment lines (use shell with wc/grep if needed; not in the graph)
  - external_calls — protocols it calls into (cross-reference the graph's "calls" edges
    against import statements to name the actual protocol, e.g. "Chainlink" not just the
    interface name), or "None"
  - function_count — total number of functions (the graph already lists every function
    per contract — count directly from it rather than re-scanning source)
  - holds_funds — does it hold ETH or ERC20 balances? (check payable functions, token mappings)
  - upgradeable — does it use a proxy pattern or UUPSUpgradeable / TransparentUpgradeableProxy?
  - risk_level — Low / Medium / High / Critical
  - risk_reason — one sentence. Weight: holds_funds and external_calls push risk up; pure
    view/library contracts push risk down.
</step>
</task>

<output_format>
Pass this to submit_findings:

{{
  "protocol_name": "Morpho Blue",

  "protocol_summary": "2-4 sentences describing what the protocol does.",

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

  "tokens": [
    {{
      "standard": "ERC20",
      "name": "USDC",
      "notes": "Primary settlement token; 6 decimals assumed; permit not used"
    }}
  ],

  "solc_version": "0.8.24",

  "architecture_notes": "Describe the contract layout, inheritance hierarchy, and the primary call flows inferred from the repo graph above and your source exploration.",

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
</output_format>

<rules>
<rule>Run STEP 0 before anything else — protocol_name must be actively searched for, not guessed.</rule>
<rule>protocol_name may be "" if genuinely undiscoverable, but you must have checked all five sources listed in STEP 0 before setting it to empty.</rule>
<rule>If documentation is missing, use web_search before guessing.</rule>
<rule>Read source files before submitting — do not rely solely on docs or the repo graph.</rule>
<rule>Every role needs at least one power.</rule>
<rule>solc_version must be a concrete version string — "unknown" will be rejected.</rule>
<rule>tokens may be empty arrays [] if truly none exist, but you must have checked the source before concluding that.</rule>
<rule>contract_risk_matrix must have one entry per in-scope contract — do not skip contracts, even simple ones.</rule>
<rule>risk_level is a triage signal for prioritization, not a vulnerability claim — do not describe specific exploits or attack paths in risk_reason, just why the contract matters.</rule>
<rule>Do not submit until all six steps are complete.</rule>
</rules>

</audit_step>"""