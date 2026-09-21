"""Trivy, ZAP, and the aggregation layer that makes three tools agree.

Parsing is tested against captured payloads rather than live binaries: Trivy is
a Go binary and ZAP is a daemon, neither is in the default install, and the part
worth testing is the normalisation, not the subprocess call.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qagent.modules.security.aggregate import scan_repository
from qagent.modules.security.base import (
    ScannerError,
    ScannerUnavailable,
    ScanResult,
    SecurityFinding,
    merge,
    rank,
)
from qagent.modules.security.trivy import parse_trivy_report, run_trivy
from qagent.modules.security.zap import ZapConfig, parse_alerts, run_zap

# --------------------------------------------------------------------- trivy


_TRIVY_PAYLOAD = {
    "Results": [
        {
            "Target": "poetry.lock",
            "Vulnerabilities": [
                {
                    "VulnerabilityID": "CVE-2024-1234",
                    "PkgName": "requests",
                    "InstalledVersion": "2.20.0",
                    "FixedVersion": "2.31.0",
                    "Severity": "CRITICAL",
                    "Title": "Requests leaks credentials on redirect",
                    "Description": "A long description.",
                    "CweIDs": ["CWE-200"],
                },
                {
                    "VulnerabilityID": "CVE-2024-9999",
                    "PkgName": "urllib3",
                    "InstalledVersion": "1.0",
                    "Severity": "UNKNOWN",
                    "Description": "Not yet scored.",
                },
            ],
        }
    ]
}


def test_trivy_findings_carry_the_fix_that_resolves_them() -> None:
    result = parse_trivy_report(_TRIVY_PAYLOAD, root="/repo")

    critical = next(f for f in result.findings if f.rule_id == "CVE-2024-1234")
    assert critical.severity == "critical"
    assert critical.fixed_version == "2.31.0"
    assert "Fixed in 2.31.0" in critical.message
    assert critical.scanner == "trivy"


def test_unscored_vulnerabilities_are_not_filed_as_info() -> None:
    """An unrated CVE is unrated because nobody has scored it, not because it
    is harmless; 'info' is where it would never be looked at again."""
    result = parse_trivy_report(_TRIVY_PAYLOAD, root="/repo")

    unknown = next(f for f in result.findings if f.rule_id == "CVE-2024-9999")
    assert unknown.severity == "medium"
    assert "No fixed version" in unknown.message


def test_the_lockfile_is_the_reported_location() -> None:
    """It is the file the developer edits to fix this."""
    result = parse_trivy_report(_TRIVY_PAYLOAD, root="/repo")

    assert all(f.path == "poetry.lock" for f in result.findings)


def test_long_descriptions_are_truncated() -> None:
    payload = {
        "Results": [
            {
                "Target": "go.sum",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-1",
                        "PkgName": "p",
                        "InstalledVersion": "1",
                        "Severity": "LOW",
                        "Description": "x" * 5000,
                    }
                ],
            }
        ]
    }

    finding = parse_trivy_report(payload, root="/repo").findings[0]

    assert len(finding.message) < 1000


def test_empty_trivy_report_is_not_an_error() -> None:
    assert parse_trivy_report({"Results": []}, root="/repo").findings == []
    assert parse_trivy_report({}, root="/repo").findings == []


def test_missing_trivy_binary_is_a_clear_unavailable(monkeypatch, tmp_path: Path) -> None:
    def _missing(*args, **kwargs):
        raise FileNotFoundError("trivy")

    monkeypatch.setattr("subprocess.run", _missing)

    with pytest.raises(ScannerUnavailable, match="not installed"):
        run_trivy(tmp_path)


def test_trivy_on_a_non_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ScannerError):
        run_trivy(tmp_path / "nope")


# ----------------------------------------------------------------------- zap


_ZAP_ALERTS = [
    {
        "pluginId": "10038",
        "name": "Content Security Policy Header Not Set",
        "riskcode": "2",
        "confidence": "3",
        "url": "http://app/login",
        "description": "No CSP header.",
        "solution": "Set the header.",
        "cweid": "693",
    },
    {
        "pluginId": "10021",
        "name": "X-Content-Type-Options Missing",
        "riskcode": "1",
        "confidence": "2",
        "url": "http://app/",
        "description": "Missing header.",
        "cweid": "-1",
    },
    {
        "pluginId": "99999",
        "name": "Scanner disbelieves this itself",
        "riskcode": "3",
        "confidence": "0",
        "url": "http://app/x",
        "description": "noise",
    },
]


def test_zap_risk_codes_become_the_shared_vocabulary() -> None:
    result = parse_alerts(_ZAP_ALERTS, target="http://app")

    by_id = {f.rule_id: f for f in result.findings}
    assert by_id["zap-10038"].severity == "medium"
    assert by_id["zap-10021"].severity == "low"
    assert all(f.scanner == "zap" for f in result.findings)


def test_zap_false_positives_are_dropped() -> None:
    """False-positive rate is this project's primary metric (ADR-0003); rows the
    scanner itself disbelieves must not attack it."""
    result = parse_alerts(_ZAP_ALERTS, target="http://app")

    assert "zap-99999" not in {f.rule_id for f in result.findings}


def test_zap_remediation_is_kept_with_the_finding() -> None:
    finding = next(f for f in parse_alerts(_ZAP_ALERTS, target="http://app").findings
                   if f.rule_id == "zap-10038")

    assert "Remediation: Set the header." in finding.message
    assert finding.cwe == ["CWE-693"]


def test_zap_placeholder_cwe_is_not_reported() -> None:
    finding = next(f for f in parse_alerts(_ZAP_ALERTS, target="http://app").findings
                   if f.rule_id == "zap-10021")

    assert finding.cwe == []


def test_the_url_is_the_location_for_a_dynamic_finding() -> None:
    """There is no source file; inventing a path would put something in the
    report that does not exist."""
    result = parse_alerts(_ZAP_ALERTS, target="http://app")

    assert {f.path for f in result.findings} == {"http://app/login", "http://app/"}


class _StubZap:
    def __init__(self, alerts: list[dict]) -> None:
        self._alerts = alerts
        self.spidered = False
        self.attacked = False

    def version(self) -> str:
        return "2.15.0"

    def spider(self, target: str) -> int:
        self.spidered = True
        return 3

    def active_scan(self, target: str) -> None:
        self.attacked = True

    def alerts(self, target: str) -> list[dict]:
        return self._alerts

    def close(self) -> None:
        pass


def test_active_scanning_is_off_unless_asked_for() -> None:
    """Attack traffic must never be sent because a flag defaulted to true."""
    zap = _StubZap(_ZAP_ALERTS)

    run_zap("http://app", client=zap)

    assert zap.spidered
    assert not zap.attacked


def test_active_scanning_happens_when_explicitly_enabled() -> None:
    zap = _StubZap(_ZAP_ALERTS)

    run_zap("http://app", active=True, client=zap)

    assert zap.attacked


def test_an_unreachable_daemon_says_how_to_start_one() -> None:
    import httpx

    from qagent.modules.security.zap import ZapClient

    class _Refusing(httpx.Client):
        def get(self, *args, **kwargs):
            raise httpx.ConnectError("refused")

    client = ZapClient(ZapConfig(base_url="http://127.0.0.1:1"), client=_Refusing())

    with pytest.raises(ScannerUnavailable, match="docker run"):
        client.version()


# ----------------------------------------------------------------- merging


def _finding(rule: str, severity: str, scanner: str, path: str = "a.py") -> SecurityFinding:
    return SecurityFinding(
        rule_id=rule, title=rule, severity=severity, path=path, line=1,
        message="", scanner=scanner,
    )


def test_the_same_finding_from_two_tools_is_counted_once() -> None:
    a = ScanResult(root="/r", findings=[_finding("CVE-1", "medium", "semgrep")])
    b = ScanResult(root="/r", findings=[_finding("CVE-1", "high", "trivy")])

    merged = merge([a, b], root="/r")

    assert len(merged.findings) == 1


def test_disagreement_resolves_to_the_higher_severity() -> None:
    """Under-reporting a real vulnerability is the more expensive mistake."""
    a = ScanResult(root="/r", findings=[_finding("CVE-1", "low", "semgrep")])
    b = ScanResult(root="/r", findings=[_finding("CVE-1", "critical", "trivy")])

    assert merge([a, b], root="/r").findings[0].severity == "critical"


def test_distinct_findings_are_kept() -> None:
    a = ScanResult(root="/r", findings=[_finding("CVE-1", "high", "trivy")])
    b = ScanResult(root="/r", findings=[_finding("CVE-2", "high", "trivy", path="b.py")])

    assert len(merge([a, b], root="/r").findings) == 2


def test_results_sort_worst_first() -> None:
    result = ScanResult(
        root="/r",
        findings=[
            _finding("a", "low", "trivy"),
            _finding("b", "critical", "trivy", path="b.py"),
            _finding("c", "medium", "trivy", path="c.py"),
        ],
    )

    assert [f.severity for f in result.sorted_findings()] == ["critical", "medium", "low"]


def test_rank_puts_an_unknown_severity_last() -> None:
    assert rank("critical") < rank("low") < rank("nonsense")


# -------------------------------------------------------------- aggregation


def test_a_missing_scanner_is_recorded_not_fatal(monkeypatch, tmp_path: Path) -> None:
    """A scan that refuses to run because one of three tools is absent is a scan
    that gets deleted from CI."""
    def _semgrep(repo_path, **kwargs):
        return ScanResult(root=str(repo_path), findings=[_finding("S1", "high", "semgrep")])

    def _trivy(repo_path, **kwargs):
        raise ScannerUnavailable("trivy is not installed")

    monkeypatch.setattr("qagent.modules.security.semgrep.run_semgrep", _semgrep)
    monkeypatch.setattr("qagent.modules.security.trivy.run_trivy", _trivy)

    result = scan_repository(tmp_path)

    assert len(result.findings) == 1
    assert "trivy" in result.skipped
    assert result.scan_errors == []


def test_a_crashing_scanner_does_not_lose_the_others(monkeypatch, tmp_path: Path) -> None:
    def _semgrep(repo_path, **kwargs):
        return ScanResult(root=str(repo_path), findings=[_finding("S1", "high", "semgrep")])

    def _trivy(repo_path, **kwargs):
        raise ScannerError("database corrupt")

    monkeypatch.setattr("qagent.modules.security.semgrep.run_semgrep", _semgrep)
    monkeypatch.setattr("qagent.modules.security.trivy.run_trivy", _trivy)

    result = scan_repository(tmp_path)

    assert len(result.findings) == 1
    assert "failed" in result.skipped["trivy"]


def test_no_scanner_available_says_the_result_means_nothing(monkeypatch, tmp_path: Path) -> None:
    """'No findings' and 'nothing ran' must never look the same."""
    def _unavailable(repo_path, **kwargs):
        raise ScannerUnavailable("not installed")

    monkeypatch.setattr("qagent.modules.security.semgrep.run_semgrep", _unavailable)
    monkeypatch.setattr("qagent.modules.security.trivy.run_trivy", _unavailable)

    result = scan_repository(tmp_path)

    assert result.findings == []
    assert result.scan_errors
    assert "says nothing" in result.scan_errors[0]
    assert set(result.skipped) == {"semgrep", "trivy"}


def test_report_serialises_with_provenance(monkeypatch, tmp_path: Path) -> None:
    def _semgrep(repo_path, **kwargs):
        return ScanResult(root=str(repo_path), findings=[_finding("S1", "high", "semgrep")])

    def _trivy(repo_path, **kwargs):
        return ScanResult(root=str(repo_path), findings=[_finding("C1", "critical", "trivy",
                                                                  path="poetry.lock")])

    monkeypatch.setattr("qagent.modules.security.semgrep.run_semgrep", _semgrep)
    monkeypatch.setattr("qagent.modules.security.trivy.run_trivy", _trivy)

    payload = scan_repository(tmp_path).to_dict()

    assert payload["counts"] == {"critical": 1, "high": 1}
    assert payload["by_scanner"] == {"trivy": 1, "semgrep": 1}
