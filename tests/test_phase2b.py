"""Phase 2B unit tests: telemetry no-op safety, drift KS logic, source health.

These cover the *pure* logic (no DB / no network) so they run anywhere. The
DB-backed seams (``compute_drift``'s loaders, ``check_source_health``'s queries)
are integration-tested via the CLI against a live Postgres in development.
"""

from __future__ import annotations

from newsroom import telemetry
from newsroom.dashboard import _summarise_health
from newsroom.eval.drift import DriftResult, decide_drift, evaluate_distributions
from newsroom.sources.health import (
    STATUS_GREEN,
    STATUS_IDLE,
    STATUS_NEW,
    STATUS_RED,
    STATUS_YELLOW,
    SourceHealthStatus,
    classify_health,
)


# --- telemetry ---------------------------------------------------------------

def test_traced_preserves_return_value_and_name():
    @telemetry.traced("demo")
    def add(run_id, x):
        return x + 1

    # functools.wraps keeps the wrapped function's identity
    assert add.__name__ == "add"
    # behaviour is unchanged whether or not tracing is active
    assert add(run_id=1, x=41) == 42


def test_traced_reraises_and_does_not_swallow():
    @telemetry.traced("boom")
    def boom(run_id):
        raise ValueError("kaboom")

    try:
        boom(run_id=5)
    except ValueError as exc:
        assert "kaboom" in str(exc)
    else:  # pragma: no cover - the call must raise
        raise AssertionError("decorated function should re-raise")


def test_record_llm_call_never_raises_without_a_span():
    # Called outside any span and with Langfuse disabled: must be a safe no-op.
    telemetry.record_llm_call(
        model="m", provider="p", in_tokens=1, out_tokens=2, cost_usd=0.01,
        input_text="in", output_text="out",
    )


def test_current_trace_id_is_none_outside_a_span():
    assert telemetry.current_trace_id() is None


def test_span_yields_a_trace_id_when_enabled():
    with telemetry.span("unit-test-span", run_id=1) as sp:
        # When OTel is installed the span is live and a trace id is available.
        if sp is not None:
            assert telemetry.current_trace_id() is not None


# --- drift -------------------------------------------------------------------

def test_decide_drift_truth_table():
    assert decide_drift(0.4, 0.9) is True          # KS over threshold
    assert decide_drift(0.1, 0.01) is True          # p under threshold
    assert decide_drift(0.1, 0.9) is False          # neither
    assert decide_drift(None, None) is False         # no data


def test_evaluate_distributions_flags_a_clear_shift():
    baseline = [0.92, 0.93, 0.91, 0.94, 0.92, 0.93]
    current = [0.55, 0.50, 0.52, 0.48, 0.60, 0.51]
    result = evaluate_distributions(baseline, current)
    assert result.insufficient is False
    assert result.drift_detected is True
    assert result.baseline_median > result.current_median


def test_evaluate_distributions_stable_distribution_is_not_drift():
    baseline = [0.90, 0.91, 0.89, 0.92, 0.90, 0.91]
    current = [0.90, 0.90, 0.91, 0.89, 0.92, 0.90]
    result = evaluate_distributions(baseline, current)
    assert result.insufficient is False
    assert result.drift_detected is False


def test_evaluate_distributions_insufficient_samples():
    result = evaluate_distributions([0.9], [0.9, 0.8], min_samples=5)
    assert result.insufficient is True
    assert result.drift_detected is False


def test_drift_result_to_dict_is_json_shaped():
    d = DriftResult(drift_detected=True, ks_statistic=0.4, p_value=0.01).to_dict()
    assert d["drift_detected"] is True
    assert set(d) >= {"ks_statistic", "p_value", "current_median", "baseline_median"}


# --- source health -----------------------------------------------------------

def test_classify_health_full_drop_is_red():
    assert classify_health(0, 10)[0] == STATUS_RED


def test_classify_health_threshold_boundary_is_red():
    status, drop = classify_health(5, 10)  # exactly 50% drop
    assert status == STATUS_RED
    assert drop == 0.5


def test_classify_health_moderate_dip_is_yellow():
    assert classify_health(7, 10)[0] == STATUS_YELLOW  # 30% drop


def test_classify_health_at_baseline_is_green():
    assert classify_health(10, 10)[0] == STATUS_GREEN


def test_classify_health_surplus_clamps_drop_to_zero():
    status, drop = classify_health(20, 10)
    assert status == STATUS_GREEN
    assert drop == 0.0


def test_classify_health_new_and_idle_without_baseline():
    assert classify_health(3, 0)[0] == STATUS_NEW
    assert classify_health(0, 0)[0] == STATUS_IDLE


def test_source_health_status_to_dict_and_alert():
    st = SourceHealthStatus("arxiv", STATUS_RED, items_today=0, baseline_avg=10.0, drop_pct=1.0)
    assert st.is_alert is True
    assert st.to_dict()["status"] == STATUS_RED


# --- dashboard ---------------------------------------------------------------

def test_summarise_health_counts_by_status():
    health = {
        "a": SourceHealthStatus("a", STATUS_GREEN, 5, 5.0, 0.0),
        "b": SourceHealthStatus("b", STATUS_RED, 0, 5.0, 1.0),
        "c": SourceHealthStatus("c", STATUS_RED, 1, 9.0, 0.88),
    }
    counts = _summarise_health(health)
    assert counts[STATUS_GREEN] == 1
    assert counts[STATUS_RED] == 2
    assert counts[STATUS_YELLOW] == 0
