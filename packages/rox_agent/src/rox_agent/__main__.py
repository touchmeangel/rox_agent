from __future__ import annotations

import argparse

from rox_core.debug_logger import default_debug_logger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rox",
        description="Solidity Smart Contract Audit Agent | rox",
    )
    subparsers = parser.add_subparsers(dest="role", required=True)

    from rox_coordinator.runner import add_coordinator_args, run_coordinator
    from rox_worker.runner import add_worker_args, run_worker

    coord_parser = subparsers.add_parser(
        "coordinator",
        help="Run the recon + planning pipeline and emit worker missions.",
    )
    add_coordinator_args(coord_parser)
    coord_parser.set_defaults(func=run_coordinator)

    worker_parser = subparsers.add_parser(
        "worker",
        help="Run a single assigned mission's verification pipeline.",
    )
    add_worker_args(worker_parser)
    worker_parser.set_defaults(func=run_worker)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    default_debug_logger.configure(args.debug)
    args.func(args)


if __name__ == "__main__":
    main()
