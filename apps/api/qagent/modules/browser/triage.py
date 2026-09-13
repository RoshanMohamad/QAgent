"""Turn a page check into a triage verdict.

`modules/browser/runner.py` already decided what counts as defect-grade evidence
(ADR-0002): a 5xx, an uncaught exception, or a console error are all unambiguous
because there is no selector to be wrong about, unlike an ordinary UI assertion.
This module just carries that decision through to a `Verdict` so a failed page
check can flow through the same classification/bug-report path as an API result.
"""

from __future__ import annotations

from qagent.modules.browser.runner import PageCheckResult
from qagent.modules.triage.classifier import FailureClass, Verdict


def classify_page_check(check: PageCheckResult) -> Verdict | None:
    if check.status == "passed":
        return None

    if check.status == "error":
        # Navigation itself never completed - the environment wasn't reachable,
        # which is a different claim than "the application is broken".
        return Verdict(
            FailureClass.ENVIRONMENT,
            0.75,
            "The page could not be loaded at all, so the application was never "
            "actually exercised.",
            evidence=[check.failure_message or "navigation failed"],
        )

    # status == "failed": a 5xx, an uncaught exception or a console error. All three
    # are the defect-grade evidence this module was built to catch (see docstring).
    return Verdict(
        FailureClass.REAL_BUG,
        0.85,
        "The page loaded but served a server error or threw while running, which "
        "is unambiguous evidence of a defect rather than a flaky assertion.",
        evidence=[check.failure_message or "page check failed"]
        + check.page_errors[:3]
        + check.console_errors[:3],
    )
