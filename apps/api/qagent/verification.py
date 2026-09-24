"""Verify a fix: re-run one defect's check on its own (CLAUDE.md section 24, step 9).

"Suggest fix" without "verify" leaves the developer to trust that their change
worked. The check that produced a defect is declarative data - a request and
its assertions, stored on the test case - so it can be replayed in isolation in
a second instead of re-running the whole scan.

**Several attempts, not one.** A single green replay is exactly what a flaky
check produces half the time, and closing a defect on that evidence would be
the flaky-test problem (section 14) reintroduced by the verification feature
itself. So a check is replayed ``attempts`` times:

- every attempt passes -> ``fixed``
- every attempt fails -> ``still_failing``
- anything mixed, or any attempt that could not reach the target -> ``inconclusive``

``inconclusive`` is a real answer, not an error: it means "do not close this on
the strength of this evidence", which is exactly what should happen.

Only API checks can be verified this way. An IDOR finding needs two identities
and a browser defect needs a page session; neither is a replayable spec. Those
close through the next full scan instead (persistence.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from qagent.modules.runner.executor import ExecutionOutcome

VERIFIABLE_KINDS = frozenset({"api_functional", "api_security"})
MAX_ATTEMPTS = 5


class NotVerifiable(ValueError):
    """This defect's check cannot be replayed on its own."""


class _Runner(Protocol):
    def execute(self, spec: dict) -> ExecutionOutcome: ...


@dataclass
class Verification:
    verdict: str  # fixed | still_failing | inconclusive
    executions: list[ExecutionOutcome] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for e in self.executions if e.status == "passed")

    @property
    def failed(self) -> int:
        return sum(1 for e in self.executions if e.status == "failed")

    @property
    def errored(self) -> int:
        return sum(1 for e in self.executions if e.status == "error")

    def summary(self) -> dict:
        return {
            "verdict": self.verdict,
            "attempts": len(self.executions),
            "passed": self.passed,
            "failed": self.failed,
            "errored": self.errored,
        }


def check_verifiable(*, kind: str, spec: dict | None, auth: str | None = None) -> dict:
    """The replayable spec, or `NotVerifiable` saying why there isn't one."""
    if kind not in VERIFIABLE_KINDS:
        raise NotVerifiable(
            f"a {kind} defect cannot be replayed on its own; run a full scan to re-check it"
        )
    if not spec or not spec.get("request") or not spec.get("assertions"):
        # Cases persisted before specs carried their assertions have only the
        # request, and a request with nothing to assert cannot tell fixed from broken.
        raise NotVerifiable(
            "this defect's check was recorded without its assertions; "
            "run a full scan once to refresh it, then verify"
        )
    if (auth or spec["request"].get("auth")) == "secondary":
        raise NotVerifiable("an access-control finding needs two identities; run a full scan")
    return spec


def verify(runner: _Runner, spec: dict, *, attempts: int = 3) -> Verification:
    if not 1 <= attempts <= MAX_ATTEMPTS:
        raise ValueError(f"attempts must be between 1 and {MAX_ATTEMPTS}")

    executions = [runner.execute(spec) for _ in range(attempts)]
    statuses = {e.status for e in executions}

    if statuses == {"passed"}:
        verdict = "fixed"
    elif statuses == {"failed"}:
        verdict = "still_failing"
    else:
        verdict = "inconclusive"
    return Verification(verdict=verdict, executions=executions)
