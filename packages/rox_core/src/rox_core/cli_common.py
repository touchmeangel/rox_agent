from __future__ import annotations

from rich.console import Console

console = Console()
ROX_COLOR = "#1dcccc"
REPO_URL = "https://github.com/touchmeangel/rox_agent"


def add_common_run_args(parser) -> None:
    """Arguments shared by every rox role."""
    parser.add_argument(
        "--work-path",
        required=True,
        help="Path to the writable workspace for build artifacts and results.",
    )
    parser.add_argument("--output", default=None, help="JSON output path.")
    parser.add_argument(
        "--debug",
        default=None,
        metavar="PATH",
        help="Log tool calls, LLM requests/responses, and lifecycle events. "
        "Pass 'stderr' (or '-') or a file path.",
    )


def aggregate_usage(results: dict) -> dict:
    totals: dict[str, dict] = {}
    for r in results.values():
        for account_key, usage in (r.model_usage or {}).items():
            entry = totals.setdefault(
                account_key,
                {
                    "model": usage.get("model", ""),
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                },
            )
            entry["calls"] += usage.get("calls", 0)
            entry["input_tokens"] += usage.get("input_tokens", 0)
            entry["output_tokens"] += usage.get("output_tokens", 0)

    total_calls = sum(m["calls"] for m in totals.values())
    total_tokens = sum(m["input_tokens"] + m["output_tokens"] for m in totals.values())

    for m in totals.values():
        m["total_tokens"] = m["input_tokens"] + m["output_tokens"]
        m["pct_of_calls"] = (
            round(100 * m["calls"] / total_calls, 2) if total_calls else 0.0
        )
        m["pct_of_tokens"] = (
            round(100 * m["total_tokens"] / total_tokens, 2) if total_tokens else 0.0
        )

    return {
        "total_calls": total_calls,
        "total_tokens": total_tokens,
        "by_model": totals,
    }


def print_role_banner(role_label: str) -> None:
    console.print()
    console.print(f"[{ROX_COLOR}]rox[/] [dim]{role_label}[/dim]  [dim]•[/dim]  ")
    console.print(f"[dim]{REPO_URL}[/dim]")
    console.print()
