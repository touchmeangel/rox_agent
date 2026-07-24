from __future__ import annotations

from rox_core.debug_logger import DebugLogger
from rox_core.reporting import Reporter
from rox_core.steps.base import BaseStep

_CONTEXT_KEYS = (
    "documentation",
    "test_coverage",
    "static_analysis",
    "fork_analysis",
    "known_issues",
    "audit_delta",
)

_INVARIANT_CATEGORIES = (
    "accounting",
    "access_control",
    "liquidity",
    "state_machine",
    "reentrancy",
    "oracle",
    "ownership",
    "supply",
    "solvency",
)


class InvariantTestingStep(BaseStep):
    name = "invariant_testing"

    AGENT_TEST_DIR = "test/rox_invariants"
    AGENT_FILE_PREFIX = "RoX_"

    def select_context(self, global_context: dict) -> dict:
        return {k: v for k, v in global_context.items() if k in _CONTEXT_KEYS}

    def environment_prompt(self) -> str:
        base = super().environment_prompt()
        return (
            base
            + f"""

\u2500\u2500 Invariant Testing Sandbox \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
Agent test directory (YOURS \u2014 read/write): {self.project_path}/{self.AGENT_TEST_DIR}/
Existing tests (READ-ONLY, never modify):  {self.project_path}/test/  (excluding above)

File naming rule \u2014 every file you create MUST be named:
  {self.AGENT_FILE_PREFIX}<ContractName>Invariant.t.sol
  e.g. {self.AGENT_FILE_PREFIX}VaultInvariant.t.sol

forge scoping \u2014 ALWAYS use one of these exact forms, never bare `forge test`:
  Your tests only : forge test --match-path "{self.AGENT_TEST_DIR}/*" -vvvv
  Existing tests  : forge test --no-match-path "{self.AGENT_TEST_DIR}/*"
  Single file     : forge test --match-path "{self.AGENT_TEST_DIR}/{self.AGENT_FILE_PREFIX}FooInvariant.t.sol" -vvvv
\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"""
        )

    def findings_schema(self) -> dict:
        return {
            "type": "object",
            "required": ["invariants", "summary"],
            "properties": {
                "invariants": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "description", "category", "status"],
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": (
                                    "snake_case property name, "
                                    "e.g. 'vault_totalAssets_ge_totalDebt'"
                                ),
                            },
                            "description": {"type": "string"},
                            "category": {
                                "type": "string",
                                "enum": list(_INVARIANT_CATEGORIES),
                            },
                            "status": {
                                "type": "string",
                                "enum": [
                                    "passed",
                                    "failed",
                                    "skipped_existing_coverage",
                                    "error",
                                ],
                            },
                            "test_file": {
                                "type": "string",
                                "description": (
                                    "Relative path of the generated file, "
                                    "or empty string if skipped."
                                ),
                            },
                            "counterexample": {
                                "type": ["string", "null"],
                                "description": (
                                    "Verbatim forge shrunk counterexample. "
                                    "Required for status 'failed'."
                                ),
                            },
                            "severity": {
                                "type": ["string", "null"],
                                "enum": ["critical", "high", "medium", "low", None],
                                "description": "Required for status 'failed'.",
                            },
                            "notes": {"type": "string"},
                        },
                    },
                },
                "summary": {
                    "type": "object",
                    "required": [
                        "total_identified",
                        "skipped_existing",
                        "generated",
                        "passed",
                        "failed",
                        "errors",
                    ],
                    "properties": {
                        "total_identified": {"type": "integer"},
                        "skipped_existing": {"type": "integer"},
                        "generated": {"type": "integer"},
                        "passed": {"type": "integer"},
                        "failed": {"type": "integer"},
                        "errors": {"type": "integer"},
                        "no_invariants_reason": {
                            "type": "string",
                            "description": (
                                "Required if invariants list is empty. Explain specifically why "
                                "no invariant tests were generated."
                            ),
                        },
                    },
                },
            },
        }

    def validate_complete(self, findings: dict) -> str | None:
        if not isinstance(findings, dict):
            return "findings must be json object (python dict)"

        invariants = findings.get("invariants")
        if invariants is None:
            return "findings.invariants must be present (use an empty list [] if there are genuinely none)"

        if not invariants:
            justification = findings.get("summary", {}).get("no_invariants_reason", "")
            if not justification or len(justification.strip()) < 20:
                return (
                    "findings.invariants is empty — if the codebase genuinely has no testable "
                    "invariants, set summary.no_invariants_reason to a specific explanation "
                    "(e.g. 'stateless library', 'all meaningful properties already covered by "
                    "existing invariant suite in test/invariants/')"
                )
            return None

        for i, inv in enumerate(invariants):
            for field in ("name", "description", "category", "status"):
                if not inv.get(field):
                    return f"invariants[{i}] is missing required field '{field}'"
            if inv["status"] == "failed":
                if not inv.get("counterexample"):
                    return (
                        f"invariants[{i}] ('{inv.get('name')}') has status 'failed' but "
                        "counterexample is empty — paste the full forge shrunk counterexample output"
                    )
                if not inv.get("severity"):
                    return (
                        f"invariants[{i}] ('{inv.get('name')}') has status 'failed' "
                        "but severity is not set"
                    )

        summary = findings.get("summary", {})
        for field in (
            "total_identified",
            "skipped_existing",
            "generated",
            "passed",
            "failed",
            "errors",
        ):
            if not isinstance(summary.get(field), int):
                return f"summary.{field} must be an integer"

        return None

    def initial_prompt(
        self, context: dict, reporter: Reporter, debug_logger: DebugLogger
    ) -> str:
        def _fmt(key: str) -> str:
            step = context.get(key, {})
            if not step or not step.get("success"):
                return "(step did not run or failed)"
            return step.get("summary") or "(no summary)"

        prior_context = "\n\n".join(
            f"=== {k.upper()} ===\n{_fmt(k)}" for k in _CONTEXT_KEYS
        )

        categories = ", ".join(_INVARIANT_CATEGORIES)

        return f"""# Invariant Testing Step

You are the final audit step. Your mission: derive system-level invariants from the
codebase, generate Foundry invariant tests for any property NOT already covered by the
existing test suite, run all generated tests, and submit a complete findings record.

Use the prior-step context below to prioritise high-risk invariants and avoid duplicating
findings already reported by static analysis or known issues.


## Context from prior steps

{prior_context}


## Workflow

Use update_plan to track your phases and mark each invariant as you go.

### Phase 1 — Setup

Create your working directory and verify the project compiles:

    shell  : mkdir -p {self.project_path}/{self.AGENT_TEST_DIR}
    forge  : build

A clean build is required before generating tests. If build fails, diagnose and fix
(or note compilation blockers) before proceeding.

### Phase 2 — Enumerate existing invariant coverage

Find every invariant function already in the project test suite (NOT your directory):

    shell: rg "function invariant_" test/ --glob "!{self.AGENT_TEST_DIR}/*" -ln
    shell: rg "function invariant_" test/ --glob "!{self.AGENT_TEST_DIR}/*" -n

Read each matching file. For every `invariant_*` function, record what property it
covers. These become entries with status "skipped_existing_coverage" in your final
findings — do NOT generate tests for them.

### Phase 3 — Derive the full invariant list

From the codebase, documentation, and prior findings, enumerate ALL meaningful system
invariants across these categories:

    {categories}

For each invariant decide:
  - Already covered by existing tests  →  mark skipped, move on
  - Not covered                        →  add to Phase 4 generation queue

Call update_plan with one entry per invariant you plan to generate.

### Phase 4 — Generate test files

Write ONE file per main contract to your agent directory.

Required path format:
    {self.AGENT_TEST_DIR}/{self.AGENT_FILE_PREFIX}<ContractName>Invariant.t.sol

Required file structure:

    // SPDX-License-Identifier: MIT
    pragma solidity ^0.8.x;

    import "forge-std/Test.sol";
    import "forge-std/StdInvariant.sol";
    // import protocol contracts

    contract {self.AGENT_FILE_PREFIX}VaultInvariant is StdInvariant, Test {{
        // ── state ────────────────────────────────────────────────
        Vault internal vault;
        // ghost variables to track expected state across calls
        uint256 internal ghost_totalDeposited;

        // ── setup ────────────────────────────────────────────────
        function setUp() public {{
            vault = new Vault(...);
            targetContract(address(vault));
            // add handler contracts for complex state transitions
        }}

        // ── invariants ───────────────────────────────────────────
        function invariant_totalAssets_ge_totalDebt() public view {{
            assertGe(vault.totalAssets(), vault.totalDebt());
        }}

        function invariant_shares_solvency() public view {{
            // ...
        }}
    }}

After writing each file, verify it compiles before moving to the next:

    forge: build --match-path "{self.AGENT_TEST_DIR}/*"

Fix all compilation errors immediately. A file that does not compile counts as status
"error" for all invariants it was meant to cover.

### Phase 5 — Run all tests and collect ALL results

Run the full agent suite once:

    forge test --match-path "{self.AGENT_TEST_DIR}/*" -vvvv 2>&1

DO NOT stop at the first failure. Let the entire suite complete and collect every result:

  - PASS                          →  status "passed"
  - FAIL with counterexample      →  status "failed", copy counterexample verbatim
  - Compilation / runtime error   →  status "error"

To isolate a single failing invariant for a clean shrunk counterexample:

    forge test --match-path "{self.AGENT_TEST_DIR}/{self.AGENT_FILE_PREFIX}VaultInvariant.t.sol" \\
               --match-test "invariant_totalAssets_ge_totalDebt" -vvvv 2>&1

Run ALL invariants before submitting. The submission schema is rejected if any
failed invariant is missing its counterexample or severity.

### Phase 6 — Submit

Once every invariant has a result, call submit_findings with the complete list.

Severity guide for failures:
  - broken core accounting invariant  →  critical
  - access control gap                →  high
  - edge-case state violation         →  medium
  - theoretical / low-impact          →  low


## Hard rules — never break these

1. Never run bare `forge test` — always scope with --match-path or --no-match-path.
2. Never write files outside `{self.AGENT_TEST_DIR}/`.
3. Every file you create must start with `{self.AGENT_FILE_PREFIX}`.
4. counterexample must be populated verbatim for every failed invariant before submitting.
5. Run ALL invariants before submitting — do not submit after the first failure.
6. summary counts must be internally consistent:
       generated == passed + failed + errors
       total_identified == skipped_existing + generated
7. If the codebase has no testable invariants (stateless library, or existing suite
   already covers everything), submit an empty invariants list [] and set
   summary.no_invariants_reason to a specific explanation — do not invent invariants
   just to have something to submit."""
