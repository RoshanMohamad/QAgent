"""The finding vocabulary every scanner reports into.

CLAUDE.md section 16's instruction is "integrate security scanners rather than
trying to reinvent everything", and the corollary is that the integration layer
has one job: make four tools that disagree about everything agree about
severity, location and identity.

They really do disagree. Semgrep says `ERROR`/`WARNING`/`INFO`; Trivy says
`CRITICAL`/`HIGH`/`MEDIUM`/`LOW`/`UNKNOWN`; ZAP says `3`/`2`/`1`/`0` and calls
it `riskcode`. A reader looking at a quality gate should never have to know
which tool produced a row, so every scanner normalises here and the gate counts
one vocabulary.

`dedupe_key` is the other half. The same missing security header is reported by
ZAP on every page, and the same vulnerable dependency by Trivy once per
lockfile that pins it; without a stable identity a report becomes a list that
nobody reads to the end.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Highest first. The gate blocks on the first two (modules/gate/policy.py).
SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")


class ScannerUnavailable(RuntimeError):
    """The scanner is not installed, not on PATH, or not reachable.

    Distinct from `ScannerError` on purpose: "you did not install Trivy" is a
    setup problem the user can fix, while "Trivy crashed" is a bug report. A
    single exception type would make the two indistinguishable in a log.
    """


class ScannerError(RuntimeError):
    """The scanner ran but failed for a reason unrelated to findings."""


def rank(severity: str) -> int:
    try:
        return SEVERITY_ORDER.index(severity)
    except ValueError:
        return len(SEVERITY_ORDER)


@dataclass
class SecurityFinding:
    rule_id: str
    title: str
    severity: str  # critical | high | medium | low | info
    path: str
    line: int
    message: str
    confidence: str | None = None
    cwe: list[str] = field(default_factory=list)
    owasp: list[str] = field(default_factory=list)
    #: Which tool produced this. Present so a reader can tell a static finding
    #: from one observed against a running application - they need very
    #: different follow-up, and merging them without a label hides that.
    scanner: str = "semgrep"
    #: Fixed version, for dependency findings. Nothing else is as actionable:
    #: "upgrade to 2.31.0" is a task, "you have a vulnerable dependency" is not.
    fixed_version: str | None = None

    def dedupe_key(self) -> tuple[str, str, int]:
        """Same rule at the same location is the same finding across re-scans."""
        return (self.rule_id, self.path, self.line)

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "severity": self.severity,
            "path": self.path,
            "line": self.line,
            "message": self.message,
            "confidence": self.confidence,
            "cwe": self.cwe,
            "owasp": self.owasp,
            "scanner": self.scanner,
            "fixed_version": self.fixed_version,
        }


@dataclass
class ScanResult:
    root: str
    findings: list[SecurityFinding] = field(default_factory=list)
    scan_errors: list[str] = field(default_factory=list)
    #: Scanners that were asked for but could not run, with the reason. Carried
    #: rather than logged and dropped: a clean security report is only
    #: meaningful alongside what actually ran to produce it.
    skipped: dict[str, str] = field(default_factory=dict)

    def counts_by_severity(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return counts

    def counts_by_scanner(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.scanner] = counts.get(finding.scanner, 0) + 1
        return counts

    def sorted_findings(self) -> list[SecurityFinding]:
        return sorted(self.findings, key=lambda f: (rank(f.severity), f.path, f.line))

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "findings": [f.to_dict() for f in self.sorted_findings()],
            "counts": self.counts_by_severity(),
            "by_scanner": self.counts_by_scanner(),
            "scan_errors": self.scan_errors,
            "skipped": self.skipped,
        }


def merge(results: list[ScanResult], *, root: str) -> ScanResult:
    """Combine several scanners' output, keeping the worst severity per finding.

    Deduplication is across tools as well as within one: Semgrep and Trivy both
    flag a vulnerable dependency often enough that a merged report would
    otherwise double-count it. When two tools disagree on severity the higher
    one wins, because under-reporting a real vulnerability is the more expensive
    mistake.
    """
    merged = ScanResult(root=root)
    by_key: dict[tuple[str, str, int], SecurityFinding] = {}

    for result in results:
        merged.scan_errors.extend(result.scan_errors)
        merged.skipped.update(result.skipped)
        for finding in result.findings:
            key = finding.dedupe_key()
            existing = by_key.get(key)
            if existing is None or rank(finding.severity) < rank(existing.severity):
                by_key[key] = finding

    merged.findings = list(by_key.values())
    return merged
