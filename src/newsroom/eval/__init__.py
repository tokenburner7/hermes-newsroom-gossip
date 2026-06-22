"""Eval harness (plan O-C2): independent quality judges + persistence/stats.

* :mod:`newsroom.eval.judge` — the pure judges (gate on the primary model, eval on
  an independent cross-family model) and the AI×Crypto rubric (amendment V7).
* :mod:`newsroom.eval.runner` — DB orchestration: load inputs, persist to ``evals``,
  set ``articles.quality_score``, and summarise gate-vs-eval agreement.
"""

from __future__ import annotations

from .judge import (
    CRITERIA,
    RUBRIC_WEIGHTS,
    RubricScores,
    eval_judge,
    eval_judge_detailed,
    gate_judge,
    gate_judge_detailed,
    weighted_total,
)
from .drift import DriftResult, compute_drift, decide_drift, evaluate_distributions
from .runner import (
    EvalInputs,
    evaluate,
    eval_stats,
    load_inputs,
    run_eval_judge,
    run_gate_judge,
    set_quality_score,
    should_sample_eval,
    store_eval,
)

__all__ = [
    # judges
    "gate_judge",
    "gate_judge_detailed",
    "eval_judge",
    "eval_judge_detailed",
    "RubricScores",
    "RUBRIC_WEIGHTS",
    "CRITERIA",
    "weighted_total",
    # drift
    "compute_drift",
    "decide_drift",
    "evaluate_distributions",
    "DriftResult",
    # runner
    "EvalInputs",
    "load_inputs",
    "run_gate_judge",
    "run_eval_judge",
    "evaluate",
    "eval_stats",
    "set_quality_score",
    "store_eval",
    "should_sample_eval",
]
