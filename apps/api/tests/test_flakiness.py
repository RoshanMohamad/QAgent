"""Flaky-test detection (qagent.modules.triage.flakiness)."""

from __future__ import annotations

from qagent.modules.triage.flakiness import (
    MIN_SAMPLES,
    QUARANTINE_THRESHOLD,
    compute_flake_rate,
    should_quarantine,
)


def test_stable_passing_history_has_zero_flake_rate() -> None:
    assert compute_flake_rate(["passed"] * 8) == 0.0


def test_stable_failing_history_has_zero_flake_rate() -> None:
    """Consistently failing is a real bug, not flakiness."""
    assert compute_flake_rate(["failed"] * 8) == 0.0


def test_alternating_history_has_high_flake_rate() -> None:
    history = ["passed", "failed", "passed", "failed", "passed"]
    assert compute_flake_rate(history) == 1.0


def test_one_flip_in_history_is_partial() -> None:
    history = ["passed", "passed", "passed", "failed"]
    assert compute_flake_rate(history) == 1 / 3


def test_short_history_is_never_flaky() -> None:
    assert compute_flake_rate([]) == 0.0
    assert compute_flake_rate(["passed"]) == 0.0


def test_window_only_considers_trailing_results() -> None:
    stable_tail = ["passed"] * 20
    noisy_head = ["passed", "failed"] * 20
    assert compute_flake_rate(noisy_head + stable_tail, window=10) == 0.0


def test_error_status_counts_as_a_failure_state() -> None:
    # Only "passed" is a pass; anything else (failed, error, skipped) is "not passed".
    history = ["passed", "error", "passed"]
    assert compute_flake_rate(history) == 1.0


def test_should_quarantine_requires_minimum_samples() -> None:
    short_history = ["passed", "failed"]
    assert not should_quarantine(short_history, flake_rate=1.0)
    assert len(short_history) < MIN_SAMPLES


def test_should_quarantine_requires_threshold() -> None:
    long_stable_history = ["passed"] * MIN_SAMPLES
    assert not should_quarantine(long_stable_history, flake_rate=0.0)


def test_should_quarantine_when_both_conditions_met() -> None:
    history = ["passed", "failed"] * MIN_SAMPLES
    rate = compute_flake_rate(history)
    assert rate >= QUARANTINE_THRESHOLD
    assert should_quarantine(history, rate)
