import argparse


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="EVM Smart Contract Audit Agent | ignite"
    )

    parser.add_argument(
        "--repo-path",
        required=True,
        help="Path to the pre-cloned repository (read-only source of truth).",
    )

    parser.add_argument(
        "--work-path",
        required=True,
        help=(
            "Path to the writable workspace. The agent stages build artifacts "
            "and writes results here."
        ),
    )

    parser.add_argument(
        "--output",
        default=None,
        help="JSON output path.",
    )

    parser.add_argument(
        "--debug",
        default=None,
        metavar="PATH",
        help=(
            "Log full tool calls, LLM requests/responses, and lifecycle events. "
            "Pass 'stderr' (or '-') or a file path."
        ),
    )

    return parser.parse_args()