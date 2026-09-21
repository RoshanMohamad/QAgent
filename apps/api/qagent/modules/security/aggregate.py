"""Run the scanners that are available and merge what they find.

The design decision worth stating: **a missing scanner is a recorded skip, not
a failure.** Semgrep, Trivy and ZAP have three different installation stories
(a pip extra, a Go binary, a daemon), and almost nobody has all three on day
one. A security scan that refuses to run because one of them is absent is a
security scan that gets removed from CI.

But a skip is never silent. `ScanResult.skipped` carries the reason, the CLI
prints it, and the API returns it, because "no findings" means something
completely different depending on whether anything actually ran.
"""

from __future__ import annotations

import logging
from pathlib import Path

from qagent.modules.security.base import (
    ScannerError,
    ScannerUnavailable,
    ScanResult,
    merge,
)

logger = logging.getLogger(__name__)

#: Scanners that read a checkout. ZAP is not here because it needs a running
#: application, not a directory - a different input entirely (ADR-0001).
STATIC_SCANNERS = ("semgrep", "trivy")


def scan_repository(
    repo_path: Path,
    *,
    scanners: tuple[str, ...] = STATIC_SCANNERS,
    timeout_seconds: float = 600.0,
) -> ScanResult:
    """Run every requested static scanner over a checkout and merge the output."""
    results: list[ScanResult] = []
    skipped: dict[str, str] = {}

    for name in scanners:
        try:
            results.append(_run_static(name, repo_path, timeout_seconds))
        except ScannerUnavailable as exc:
            # Expected and common. Recorded so a clean report can be read
            # alongside what produced it.
            logger.info("security scanner %s unavailable: %s", name, exc)
            skipped[name] = str(exc)
        except ScannerError as exc:
            # The scanner is installed and broke. Worth a warning, but still
            # not worth losing the other scanners' findings over.
            logger.warning("security scanner %s failed: %s", name, exc)
            skipped[name] = f"failed: {exc}"

    merged = merge(results, root=str(repo_path))
    merged.skipped.update(skipped)

    if not results:
        merged.scan_errors.append(
            "no security scanner could run; this result says nothing about the project"
        )

    logger.info(
        "security scan: %d finding(s) from %d scanner(s), %d skipped",
        len(merged.findings),
        len(results),
        len(skipped),
    )
    return merged


def _run_static(name: str, repo_path: Path, timeout_seconds: float) -> ScanResult:
    if name == "semgrep":
        from qagent.modules.security.semgrep import run_semgrep

        return run_semgrep(repo_path, timeout_seconds=timeout_seconds)

    if name == "trivy":
        from qagent.modules.security.trivy import run_trivy

        return run_trivy(repo_path, timeout_seconds=timeout_seconds)

    raise ScannerError(f"unknown scanner {name!r}")
