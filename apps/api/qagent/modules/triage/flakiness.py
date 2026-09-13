"""Flaky-test detection at the level of a test case, not a single result.

`classifier.flipped_recently` labels one failing result as flaky by looking at a
short trailing window fetched fresh at triage time. That says something about the
result in front of it, not about the case: a case that is unreliable but happens to
pass on its latest run looks perfectly healthy there. This module tracks the
property on `TestCase.flake_rate`/`quarantined` instead, updated after every run
regardless of whether that run passed, so a flaky case stays visible even mid-streak.
"""

from __future__ import annotations

#: Trailing results considered. Older history shouldn't keep a case quarantined
#: forever once it stabilises.
WINDOW = 10

#: Minimum samples before a rate is trusted. Two runs isn't evidence either way.
MIN_SAMPLES = 4

#: Flake rate at/above which a case is quarantined automatically.
QUARANTINE_THRESHOLD = 0.3


def compute_flake_rate(history: list[str], window: int = WINDOW) -> float:
    """Fraction of consecutive result pairs, within the trailing window, whose
    pass/fail state changed. 0.0 is fully stable; close to 1.0 flips almost every run.

    ``history`` is oldest-first, matching persistence.case_result_history.
    """
    recent = history[-window:]
    if len(recent) < 2:
        return 0.0
    transitions = sum(
        1 for a, b in zip(recent, recent[1:], strict=False) if (a == "passed") != (b == "passed")
    )
    return transitions / (len(recent) - 1)


def should_quarantine(
    history: list[str], flake_rate: float, min_samples: int = MIN_SAMPLES
) -> bool:
    return len(history) >= min_samples and flake_rate >= QUARANTINE_THRESHOLD
