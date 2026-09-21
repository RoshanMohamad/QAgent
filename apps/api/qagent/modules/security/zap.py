"""Dynamic application security testing via OWASP ZAP (CLAUDE.md §16).

The third kind of scanner, and the only one that looks at the *running*
application. Semgrep reads source, Trivy reads lockfiles; ZAP sends requests
and reads what comes back, which is the only way to see a missing security
header, a cookie without `HttpOnly`, or a reflected parameter.

Talked to over its REST API rather than by shelling out. ZAP is normally run as
a daemon or a container, not as a one-shot binary, and it is already speaking
HTTP - adding a subprocess wrapper around a thing that has an API would be
strictly worse.

Two deliberate limits:

**Passive scanning only, by default.** ZAP's active scanner sends attack
traffic: injection payloads, traversal attempts, and requests designed to
mutate state. Pointing that at anything without explicit authorisation is not a
test, and a QA tool that does it because a flag defaulted to true is a liability.
``active=True`` exists and is documented; it is never the default.

**The spider is bounded.** An unbounded crawl of an application that links to
the internet is a denial-of-service with extra steps.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from qagent.modules.security.base import (
    ScannerError,
    ScannerUnavailable,
    ScanResult,
    SecurityFinding,
)

logger = logging.getLogger(__name__)

#: ZAP reports risk as a numeric `riskcode`. Note that 3 ("High") maps to our
#: "high" and nothing maps to "critical": ZAP has no such level, and inventing
#: one would put DAST findings above Semgrep's injection results for no reason
#: a reader could check.
_RISK_MAP = {"3": "high", "2": "medium", "1": "low", "0": "info"}

#: ZAP's confidence scale, for the same normalisation reason.
_CONFIDENCE_MAP = {"3": "high", "2": "medium", "1": "low", "0": "false positive"}


@dataclass
class ZapConfig:
    base_url: str = "http://127.0.0.1:8090"
    api_key: str | None = None
    timeout_seconds: float = 30.0
    #: How long to let the spider and scanner run before giving up on them.
    max_wait_seconds: float = 300.0
    #: Pages the spider may visit. Bounded: see the module docstring.
    max_children: int = 50


class ZapClient:
    """The handful of ZAP API calls this integration needs."""

    def __init__(self, config: ZapConfig | None = None, *, client: httpx.Client | None = None):
        self.config = config or ZapConfig()
        self._client = client or httpx.Client(timeout=self.config.timeout_seconds)
        self._owns_client = client is None

    def __enter__(self) -> ZapClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _get(self, path: str, **params: str) -> dict:
        if self.config.api_key:
            params["apikey"] = self.config.api_key
        url = f"{self.config.base_url.rstrip('/')}/JSON/{path}"
        try:
            response = self._client.get(url, params=params)
            response.raise_for_status()
        except httpx.ConnectError as exc:
            raise ScannerUnavailable(
                f"no ZAP daemon reachable at {self.config.base_url} - start one with "
                "`docker run -p 8090:8090 zaproxy/zap-stable zap.sh -daemon "
                "-host 0.0.0.0 -port 8090 -config api.disablekey=true`"
            ) from exc
        except httpx.HTTPError as exc:
            raise ScannerError(f"ZAP API call {path} failed: {exc}") from exc
        return response.json()

    def version(self) -> str:
        return str(self._get("core/view/version/").get("version", "unknown"))

    def spider(self, target: str) -> int:
        """Crawl the target, returning how many URLs were found."""
        started = self._get(
            "spider/action/scan/", url=target, maxChildren=str(self.config.max_children)
        )
        scan_id = str(started.get("scan", ""))
        self._await_completion("spider/view/status/", scan_id, what="spider")
        results = self._get("spider/view/results/", scanId=scan_id)
        return len(results.get("results") or [])

    def active_scan(self, target: str) -> None:
        """Send attack traffic. Only ever called with explicit opt-in."""
        started = self._get("ascan/action/scan/", url=target)
        self._await_completion("ascan/view/status/", str(started.get("scan", "")), what="ascan")

    def alerts(self, target: str) -> list[dict]:
        payload = self._get("core/view/alerts/", baseurl=target)
        return list(payload.get("alerts") or [])

    def _await_completion(self, status_path: str, scan_id: str, *, what: str) -> None:
        deadline = time.monotonic() + self.config.max_wait_seconds
        while time.monotonic() < deadline:
            status = self._get(status_path, scanId=scan_id).get("status", "0")
            if str(status) == "100":
                return
            time.sleep(2.0)
        # A timeout is not fatal: ZAP keeps whatever it found so far, and
        # partial results beat discarding a five-minute scan.
        logger.warning("%s did not finish within %ss; using partial results", what, deadline)


def parse_alerts(alerts: list[dict], *, target: str) -> ScanResult:
    """Turn ZAP alerts into findings. Separate from the client so the mapping
    is testable without a daemon."""
    result = ScanResult(root=target)

    for alert in alerts:
        risk = str(alert.get("riskcode", "0"))
        severity = _RISK_MAP.get(risk, "info")
        confidence = _CONFIDENCE_MAP.get(str(alert.get("confidence", "")), None)

        # ZAP marks its own false positives with confidence 0. Reporting those
        # would attack the metric this project treats as primary - false
        # positive rate (ADR-0003) - with rows the scanner itself disbelieves.
        if confidence == "false positive":
            continue

        description = str(alert.get("description") or "").strip()
        solution = str(alert.get("solution") or "").strip()
        message = description
        if solution:
            message = f"{description}\n\nRemediation: {solution}".strip()

        cwe = str(alert.get("cweid", "") or "")
        result.findings.append(
            SecurityFinding(
                rule_id=f"zap-{alert.get('pluginId', 'unknown')}",
                title=str(alert.get("name") or "ZAP alert"),
                severity=severity,
                # The URL is the location for a dynamic finding: there is no
                # source file to point at, and pretending otherwise would put a
                # path in the report that does not exist.
                path=str(alert.get("url") or target),
                line=0,
                message=message[:2000],
                confidence=confidence,
                cwe=[f"CWE-{cwe}"] if cwe and cwe != "-1" else [],
                scanner="zap",
            )
        )

    return result


def run_zap(
    target: str,
    *,
    config: ZapConfig | None = None,
    active: bool = False,
    client: ZapClient | None = None,
) -> ScanResult:
    """Spider ``target``, collect passive alerts, and optionally attack it.

    ``active=False`` is the default and means ZAP only analyses traffic the
    spider generated. Set it to True only against a system you are authorised
    to attack: it sends injection and traversal payloads and can mutate state.
    """
    owned = client is None
    zap = client or ZapClient(config)

    try:
        logger.info("zap %s: spidering %s", zap.version(), target)
        found = zap.spider(target)
        logger.info("zap spider found %d url(s)", found)

        if active:
            logger.warning("zap active scan enabled: sending attack traffic to %s", target)
            zap.active_scan(target)

        result = parse_alerts(zap.alerts(target), target=target)
    finally:
        if owned:
            zap.close()

    logger.info("zap reported %d finding(s) for %s", len(result.findings), target)
    return result
