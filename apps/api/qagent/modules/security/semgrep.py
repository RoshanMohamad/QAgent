"""Static application security testing via Semgrep (CLAUDE.md section 16).

CLAUDE.md's own guidance for this feature is "integrate security scanners rather
than trying to reinvent everything" - so this module shells out to Semgrep, a
scanner maintained by people who track OWASP/CWE rule coverage full time, and
turns its findings into the same Severity vocabulary every other defect source
in this platform uses. It is static analysis only: it parses source, it never
executes the repository's code, so none of the sandboxing section 22 requires
for the runner/browser/explorer stages applies here.

Semgrep is an external binary, not a Python import, so "not installed" surfaces
as a subprocess FileNotFoundError rather than a ModuleNotFoundError - the same
shape of problem the browser/explorer modules solve for a missing Playwright,
solved the same way: a clear, typed exception instead of a stack trace.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


class SemgrepUnavailable(RuntimeError):
    """Semgrep is not installed or not on PATH."""


class SemgrepError(RuntimeError):
    """Semgrep ran but exited with an error unrelated to findings."""


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

    def dedupe_key(self) -> tuple[str, str, int]:
        """Same rule at the same location is the same finding across re-scans."""
        return (self.rule_id, self.path, self.line)


@dataclass
class ScanResult:
    root: str
    findings: list[SecurityFinding] = field(default_factory=list)
    scan_errors: list[str] = field(default_factory=list)

    def counts_by_severity(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return counts


_SEVERITY_MAP = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}

#: OWASP Top 10 (2021) categories bumped from high to critical: broken access
#: control and injection are the two classes CLAUDE.md's own example (SQL
#: injection) calls out by name, and "high" would bury them under routine findings.
_CRITICAL_OWASP_CODES = {"A01", "A03"}


def _map_severity(raw: dict) -> str:
    extra = raw.get("extra") or {}
    base = _SEVERITY_MAP.get(str(extra.get("severity", "")).upper(), "medium")

    metadata = extra.get("metadata") or {}
    owasp_codes = {
        str(o).split(":")[0].strip() for o in (metadata.get("owasp") or []) if isinstance(o, str)
    }
    if base == "high" and owasp_codes & _CRITICAL_OWASP_CODES:
        return "critical"
    return base


def _parse_result(raw: dict) -> SecurityFinding:
    extra = raw.get("extra") or {}
    metadata = extra.get("metadata") or {}
    check_id = raw.get("check_id", "unknown")

    return SecurityFinding(
        rule_id=check_id,
        title=check_id.rsplit(".", 1)[-1].replace("-", " "),
        severity=_map_severity(raw),
        path=raw.get("path", "unknown"),
        line=int((raw.get("start") or {}).get("line", 0)),
        message=str(extra.get("message", "")).strip(),
        confidence=metadata.get("confidence"),
        cwe=list(metadata.get("cwe") or []),
        owasp=list(metadata.get("owasp") or []),
    )


def run_semgrep(
    repo_path: Path,
    *,
    config: str = "auto",
    timeout_seconds: float = 300.0,
) -> ScanResult:
    """Run Semgrep against ``repo_path`` and return structured findings.

    ``config="auto"`` pulls Semgrep's registry rules for whatever languages it
    detects in the tree, so there's useful output on day one with no per-project
    rule curation. Requires the optional ``security`` extra
    (``pip install qagent[security]``) or a system Semgrep install.
    """
    if not repo_path.is_dir():
        raise SemgrepError(f"not a directory: {repo_path}")

    try:
        # Fixed argv, no shell, PATH lookup of `semgrep` is intended.
        completed = subprocess.run(  # noqa: S603
            ["semgrep", "--config", config, "--json", "--quiet", str(repo_path)],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SemgrepUnavailable(
            "semgrep is not installed or not on PATH (pip install qagent[security])"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SemgrepError(f"semgrep timed out after {timeout_seconds}s") from exc

    if not completed.stdout:
        raise SemgrepError(
            completed.stderr.strip() or f"semgrep exited {completed.returncode} with no output"
        )

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SemgrepError(f"could not parse semgrep output: {exc}") from exc

    findings = [_parse_result(r) for r in payload.get("results", [])]
    scan_errors = [str(e.get("message", e)) for e in payload.get("errors", [])]
    logger.info("semgrep found %d finding(s) under %s", len(findings), repo_path)

    return ScanResult(root=str(repo_path), findings=findings, scan_errors=scan_errors)
