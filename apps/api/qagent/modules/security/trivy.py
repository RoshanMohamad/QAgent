"""Dependency and container vulnerability scanning via Trivy (CLAUDE.md §16).

Semgrep finds bugs in code the project wrote. Trivy finds known CVEs in code
the project *imported*, which in most applications is the overwhelming majority
of the bytes being shipped and the source of most real-world compromise. The
two are complementary and neither substitutes for the other.

Like Semgrep, this shells out to a binary rather than reimplementing anything:
the value of a vulnerability scanner is its database, updated daily by people
who do nothing else, and that is precisely the part that cannot be vendored.

Trivy's filesystem scanner reads lockfiles (`poetry.lock`, `package-lock.json`,
`go.sum`, ...) and matches them against its advisory database. It does not
execute the project, install anything, or resolve dependencies over the
network, so ADR-0006's rule - parse a checkout, never run its tooling - holds.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from qagent.modules.security.base import (
    ScannerError,
    ScannerUnavailable,
    ScanResult,
    SecurityFinding,
)

logger = logging.getLogger(__name__)

#: Trivy's severities map almost one-to-one onto ours. `UNKNOWN` becomes
#: "medium" rather than "info": an unrated CVE is unrated because nobody has
#: scored it yet, not because it is harmless, and filing it under "info" is how
#: it never gets looked at.
_SEVERITY_MAP = {
    "CRITICAL": "critical",
    "HIGH": "high",
    "MEDIUM": "medium",
    "LOW": "low",
    "UNKNOWN": "medium",
}


def _parse_vulnerability(vuln: dict, target: str) -> SecurityFinding:
    cve = str(vuln.get("VulnerabilityID", "unknown"))
    package = str(vuln.get("PkgName", "unknown"))
    installed = str(vuln.get("InstalledVersion", "?"))
    fixed = vuln.get("FixedVersion") or None

    title = str(vuln.get("Title") or "").strip() or f"{cve} in {package}"
    message = str(vuln.get("Description") or "").strip()
    # Descriptions run to paragraphs and land in a bug report and an issue
    # tracker; the CVE link carries the full text for anyone who wants it.
    if len(message) > 600:
        message = message[:600].rstrip() + "..."

    summary = f"{package} {installed} is affected by {cve}."
    summary += f" Fixed in {fixed}." if fixed else " No fixed version is published yet."

    return SecurityFinding(
        rule_id=cve,
        title=title,
        severity=_SEVERITY_MAP.get(str(vuln.get("Severity", "")).upper(), "medium"),
        # The lockfile is the file a developer edits to fix this, so it is the
        # right "location" even though the vulnerable code lives elsewhere.
        path=target,
        line=0,
        message=f"{summary} {message}".strip(),
        cwe=list(vuln.get("CweIDs") or []),
        scanner="trivy",
        fixed_version=fixed,
    )


def parse_trivy_report(payload: dict, *, root: str) -> ScanResult:
    """Turn Trivy's JSON into findings. Split out so it is testable without the
    binary, which is the only way this gets covered in CI at all."""
    result = ScanResult(root=root)

    for target in payload.get("Results") or []:
        target_name = str(target.get("Target", "unknown"))
        for vuln in target.get("Vulnerabilities") or []:
            result.findings.append(_parse_vulnerability(vuln, target_name))

        # Trivy reports per-target failures inline rather than failing the run.
        for misconfig in target.get("MisconfSummary") or []:
            if isinstance(misconfig, str):
                result.scan_errors.append(misconfig)

    return result


def run_trivy(
    repo_path: Path,
    *,
    timeout_seconds: float = 600.0,
    ignore_unfixed: bool = False,
    severities: str = "CRITICAL,HIGH,MEDIUM,LOW",
) -> ScanResult:
    """Scan a checkout's dependencies for known vulnerabilities.

    ``ignore_unfixed`` is off by default. A vulnerability with no published fix
    is still a vulnerability, and hiding it means the team learns about it on
    the day it is exploited rather than the day it is disclosed - they may still
    want to drop the dependency, pin around it, or add a mitigation.
    """
    if not repo_path.is_dir():
        raise ScannerError(f"not a directory: {repo_path}")

    argv = [
        "trivy",
        "filesystem",
        "--format",
        "json",
        "--quiet",
        "--scanners",
        "vuln",
        "--severity",
        severities,
    ]
    if ignore_unfixed:
        argv.append("--ignore-unfixed")
    argv.append(str(repo_path))

    try:
        # Fixed argv, no shell; PATH lookup of `trivy` is intended.
        completed = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, timeout=timeout_seconds, check=False
        )
    except FileNotFoundError as exc:
        raise ScannerUnavailable(
            "trivy is not installed or not on PATH - see "
            "https://trivy.dev/latest/getting-started/installation/"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ScannerError(f"trivy timed out after {timeout_seconds}s") from exc

    if not completed.stdout.strip():
        raise ScannerError(
            completed.stderr.strip() or f"trivy exited {completed.returncode} with no output"
        )

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ScannerError(f"could not parse trivy output: {exc}") from exc

    result = parse_trivy_report(payload, root=str(repo_path))
    logger.info("trivy found %d vulnerabilit(ies) under %s", len(result.findings), repo_path)
    return result
