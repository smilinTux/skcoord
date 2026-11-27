"""Package entry point for ``python -m skcoord.scheduler_truth``.

Runs the read-only SchedulerTruthV1 CLI and exits with its return code.
"""
import sys


def _run_cli() -> int:
    from skcoord.scheduler_truth_cli import main

    return main()


if __name__ == "__main__":
    sys.exit(_run_cli())
