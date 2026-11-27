"""Package marker for the ``skcoord.scheduler_truth`` CLI entry point.

``python -m skcoord.scheduler_truth`` resolves through this package; the CLI
logic lives in ``skcoord.scheduler_truth_cli`` and is imported lazily by
``__main__`` so importing the package stays light and cycle-free.

When imported as a plain module (``import skcoord.scheduler_truth``) it also
re-exports the SchedulerTruthV1 API from the implementation module so that
``import skcoord.scheduler_truth; skcoord.scheduler_truth.evaluate_scheduler_truth``
keeps working.
"""
import importlib


def __getattr__(name):
    # Re-export the truth implementation lazily, only when the name is asked
    # for. This keeps ``import skcoord.scheduler_truth`` returning the
    # SchedulerTruthV1 API while ``python -m skcoord.scheduler_truth`` runs
    # ``__main__`` without the package ``__init__`` importing the CLI module.
    impl = importlib.import_module("skcoord._scheduler_truth_impl")
    return getattr(impl, name)


__all__ = [
    "PRIMARY_REASONS",
    "REASON_ACTIONS",
    "SchedulerCardFacts",
    "SchedulerTruthV1",
    "ShadowComparison",
    "compare_shadow",
    "evaluate_scheduler_truth",
]
