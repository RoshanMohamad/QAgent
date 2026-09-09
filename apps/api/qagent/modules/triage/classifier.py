"""Failure classification (CLAUDE.md section 14).

A failing test is not a defect. Roughly half of red results in any real suite are the
suite's own fault, and a QA tool that reports all of them as bugs is discarded within
a week. This module is the platform's core value: it decides *why* something failed.

The design is rules-first for a specific reason. A deterministic classifier is
auditable, free, instant, and stable across runs, which means its accuracy can be
measured against the fixture labels in packages/fixtures. The model is consulted only
where the rules are genuinely uncertain, and its answer is recorded as an override so
the two can be compared in the eval harness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class FailureClass(StrEnum):
    REAL_BUG = "real_bug"
    FLAKY_TEST = "flaky_test"
    ENVIRONMENT = "environment"
    NETWORK = "network"
    DEPENDENCY = "dependency"
    TEST_DATA = "test_data"
    BAD_ASSERTION = "bad_assertion"
    UNKNOWN = "unknown"


#: Confidence below this consults the model, when one is configured.
ARBITRATION_THRESHOLD = 0.70

_STACK_TRACE = re.compile(
    r"(Traceback \(most recent call last\)"
    r"|at [\w.$]+\(.*\.java:\d+\)"
    r"|^\s*File \".*\", line \d+"
    r"|\bat Object\.<anonymous>"
    r"|goroutine \d+ \[running\])",
    re.MULTILINE,
)

_DEPENDENCY_HINTS = re.compile(
    r"(ECONNREFUSED|could not connect|connection refused|no such host"
    r"|OperationalError|psycopg|SQLSTATE|redis\.exceptions|MongoNetworkError"
    r"|upstream connect error|502 Bad Gateway|503 Service Unavailable)",
    re.IGNORECASE,
)

_TEST_DATA_HINTS = re.compile(
    r"(not found|does not exist|no rows|empty result|unknown id)", re.IGNORECASE
)


@dataclass
class Signals:
    """Everything the classifier reasons over, extracted once."""

    status: int | None
    expected_statuses: list[int]
    is_transport_error: bool
    is_timeout: bool
    has_stack_trace: bool
    has_dependency_hint: bool
    has_test_data_hint: bool
    duration_ms: int
    was_negative_case: bool
    auth_configured: bool
    is_auth_probe: bool = False
    recent_history: list[str] = field(default_factory=list)

    @property
    def is_server_error(self) -> bool:
        return self.status is not None and 500 <= self.status < 600

    @property
    def is_auth_status(self) -> bool:
        return self.status in (401, 403)

    @property
    def flipped_recently(self) -> bool:
        """The same case both passed and failed in its recent history.

        Without a corresponding code change that is the signature of flakiness, and
        it is why test_results keeps a per-case sequence rather than a boolean.
        """
        recent = self.recent_history[-6:]
        return "passed" in recent and "failed" in recent


@dataclass
class Verdict:
    failure_class: FailureClass
    confidence: float
    reason: str
    source: str = "rules"
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "failure_class": self.failure_class.value,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "source": self.source,
            "evidence": self.evidence,
        }


def extract_signals(
    *,
    spec: dict,
    request: dict,
    response: dict,
    failure_message: str | None,
    auth_configured: bool = False,
    recent_history: list[str] | None = None,
) -> Signals:
    body = response.get("body_text") or ""
    combined = f"{body}\n{failure_message or ''}"

    expected: list[int] = []
    for assertion in spec.get("assertions", []):
        if assertion.get("type") == "status_in":
            expected = list(assertion.get("value") or [])
            break

    # A negative case is one that deliberately sends bad input and expects a 4xx.
    was_negative = bool(expected) and all(400 <= code < 500 for code in expected)

    # An auth probe deliberately strips or forges credentials and expects rejection.
    # It must be distinguished from an ordinary failure, because a *successful*
    # response to one is an authentication bypass rather than a fussy assertion.
    is_auth_probe = (
        request.get("auth") == "none" and bool(expected) and set(expected).issubset({401, 403})
    )

    error_kind = response.get("error")
    return Signals(
        status=response.get("status"),
        expected_statuses=expected,
        is_transport_error=error_kind is not None,
        is_timeout=error_kind == "timeout",
        has_stack_trace=bool(_STACK_TRACE.search(combined)),
        has_dependency_hint=bool(_DEPENDENCY_HINTS.search(combined)),
        has_test_data_hint=bool(_TEST_DATA_HINTS.search(combined)),
        duration_ms=response.get("duration_ms", 0),
        was_negative_case=was_negative,
        auth_configured=auth_configured,
        is_auth_probe=is_auth_probe,
        recent_history=recent_history or [],
    )


def classify(signals: Signals) -> Verdict:
    """Return the most specific defensible classification.

    Ordering matters: infrastructure explanations are considered before blaming the
    application, because a false 'real bug' costs far more trust than a false
    'environment failure'.
    """
    # --- transport-level: the request never got a verdict from the application ---
    if signals.is_timeout:
        return Verdict(
            FailureClass.NETWORK,
            0.80,
            "The request timed out, so the application never returned a verdict. "
            "This is a network or capacity condition unless it reproduces consistently.",
            evidence=[f"timeout after {signals.duration_ms}ms"],
        )

    if signals.is_transport_error:
        return Verdict(
            FailureClass.ENVIRONMENT,
            0.85,
            "The connection could not be established, so the environment was not "
            "reachable. The application itself was never exercised.",
            evidence=["transport error before any response"],
        )

    if signals.has_dependency_hint and signals.is_server_error:
        return Verdict(
            FailureClass.DEPENDENCY,
            0.82,
            "The response carries a downstream connection failure, so a dependency "
            "(database, cache or upstream service) was unavailable rather than the "
            "handler being wrong.",
            evidence=["dependency error signature in response body"],
        )

    # --- authentication bypass ---
    # Checked before every test-side explanation. A request that deliberately carried
    # no credentials, or a forged token, and was nonetheless served is an access
    # control failure. Treating this as an over-specified assertion (which a naive
    # "2xx means the app is fine" rule does) hides the most severe class of defect
    # the platform can find.
    if signals.is_auth_probe and signals.status is not None and 200 <= signals.status < 300:
        return Verdict(
            FailureClass.REAL_BUG,
            0.93,
            "The endpoint served a request that carried no valid credentials. The "
            "specification advertises this operation as protected, so authentication "
            "is advertised but not enforced.",
            evidence=[
                f"status {signals.status} with credentials removed",
                f"specification expects {signals.expected_statuses}",
            ],
        )

    # --- flakiness: history contradicts this result ---
    if signals.flipped_recently and not signals.is_server_error:
        return Verdict(
            FailureClass.FLAKY_TEST,
            0.72,
            "This case has both passed and failed recently without a corresponding "
            "change, which is the signature of a flaky test rather than a defect.",
            evidence=[f"recent history: {', '.join(signals.recent_history[-6:])}"],
        )

    # --- genuine application defects ---
    if signals.is_server_error and signals.has_stack_trace:
        return Verdict(
            FailureClass.REAL_BUG,
            0.95,
            "The handler raised an unhandled exception and leaked a stack trace. "
            "A 5xx with a trace is an application defect by definition.",
            evidence=[f"status {signals.status}", "stack trace present in response"],
        )

    if signals.is_server_error and signals.was_negative_case:
        return Verdict(
            FailureClass.REAL_BUG,
            0.90,
            "Deliberately invalid input produced a server error where the "
            "specification requires a client error. The handler is missing input "
            "validation.",
            evidence=[
                f"status {signals.status}",
                f"specification expects {signals.expected_statuses}",
            ],
        )

    if signals.is_server_error:
        return Verdict(
            FailureClass.REAL_BUG,
            0.85,
            "The application returned a server error for a request it documents as supported.",
            evidence=[f"status {signals.status}"],
        )

    # --- test-side explanations ---
    # Applies to negative cases too: a malformed-input check that never got past the
    # auth layer tested nothing, whatever it was written to assert.
    if signals.is_auth_status and not signals.auth_configured and not signals.is_auth_probe:
        return Verdict(
            FailureClass.ENVIRONMENT,
            0.78,
            "The endpoint rejected the request as unauthenticated and this "
            "environment has no credentials configured. The test could not reach the "
            "behaviour it was written to check.",
            evidence=[f"status {signals.status}", "no auth configured for environment"],
        )

    if signals.status == 404 and not signals.was_negative_case and signals.has_test_data_hint:
        return Verdict(
            FailureClass.TEST_DATA,
            0.70,
            "The target resource does not exist in this environment, so the fixture "
            "the case depends on is missing rather than the handler being wrong.",
            evidence=["status 404 with a not-found body on a positive case"],
        )

    if (
        signals.status is not None
        and 200 <= signals.status < 300
        and signals.expected_statuses
        and signals.status not in signals.expected_statuses
    ):
        return Verdict(
            FailureClass.BAD_ASSERTION,
            0.68,
            "The application succeeded but with a different 2xx code than the "
            "assertion allows. This is very likely an over-specified test rather than "
            "a defect.",
            evidence=[f"got {signals.status}, expected one of {signals.expected_statuses}"],
        )

    return Verdict(
        FailureClass.UNKNOWN,
        0.35,
        "No rule matched this failure with confidence. Escalating for review.",
        evidence=[f"status {signals.status}"],
    )
