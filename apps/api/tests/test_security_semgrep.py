"""Semgrep wrapper: parsing and severity mapping tested against a fake subprocess,
so these tests need neither the ``semgrep`` binary nor network access.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from qagent.modules.security.semgrep import (
    SemgrepError,
    SemgrepUnavailable,
    run_semgrep,
)

SQLI_RESULT = {
    "check_id": "python.django.security.injection.sql-injection-using-raw",
    "path": "app/views.py",
    "start": {"line": 42, "col": 1},
    "extra": {
        "message": "User input flows into a raw SQL query.",
        "severity": "ERROR",
        "metadata": {"confidence": "HIGH", "cwe": ["CWE-89"], "owasp": ["A03:2021 - Injection"]},
    },
}

WARNING_RESULT = {
    "check_id": "generic.secrets.security.detected-generic-secret",
    "path": "config/settings.py",
    "start": {"line": 5, "col": 1},
    "extra": {
        "message": "Possible hardcoded secret.",
        "severity": "WARNING",
        "metadata": {"confidence": "MEDIUM"},
    },
}

INFO_RESULT = {
    "check_id": "python.lang.best-practice.unused-import",
    "path": "app/utils.py",
    "start": {"line": 1, "col": 1},
    "extra": {"message": "Unused import.", "severity": "INFO", "metadata": {}},
}


def _fake_completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["semgrep"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_error_severity_with_owasp_injection_is_critical(monkeypatch, tmp_path: Path) -> None:
    payload = {"results": [SQLI_RESULT], "errors": []}
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: _fake_completed(stdout=json.dumps(payload))
    )

    result = run_semgrep(tmp_path)
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.severity == "critical"
    assert finding.rule_id == SQLI_RESULT["check_id"]
    assert finding.path == "app/views.py"
    assert finding.line == 42
    assert finding.cwe == ["CWE-89"]


def test_warning_severity_maps_to_medium(monkeypatch, tmp_path: Path) -> None:
    payload = {"results": [WARNING_RESULT], "errors": []}
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: _fake_completed(stdout=json.dumps(payload))
    )

    result = run_semgrep(tmp_path)
    assert result.findings[0].severity == "medium"


def test_info_severity_maps_to_low(monkeypatch, tmp_path: Path) -> None:
    payload = {"results": [INFO_RESULT], "errors": []}
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: _fake_completed(stdout=json.dumps(payload))
    )

    result = run_semgrep(tmp_path)
    assert result.findings[0].severity == "low"


def test_error_without_critical_owasp_tag_stays_high(monkeypatch, tmp_path: Path) -> None:
    raw = {
        "check_id": "python.lang.security.eval-detected",
        "path": "app/x.py",
        "start": {"line": 1},
        "extra": {"message": "eval() used.", "severity": "ERROR", "metadata": {}},
    }
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: _fake_completed(stdout=json.dumps({"results": [raw], "errors": []})),
    )

    result = run_semgrep(tmp_path)
    assert result.findings[0].severity == "high"


def test_dedupe_key_is_rule_path_and_line(monkeypatch, tmp_path: Path) -> None:
    payload = {"results": [SQLI_RESULT], "errors": []}
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: _fake_completed(stdout=json.dumps(payload))
    )

    finding = run_semgrep(tmp_path).findings[0]
    assert finding.dedupe_key() == (SQLI_RESULT["check_id"], "app/views.py", 42)


def test_counts_by_severity(monkeypatch, tmp_path: Path) -> None:
    payload = {"results": [SQLI_RESULT, WARNING_RESULT, INFO_RESULT], "errors": []}
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: _fake_completed(stdout=json.dumps(payload))
    )

    counts = run_semgrep(tmp_path).counts_by_severity()
    assert counts == {"critical": 1, "medium": 1, "low": 1}


def test_semgrep_scan_errors_are_captured_but_not_fatal(monkeypatch, tmp_path: Path) -> None:
    payload = {"results": [], "errors": [{"message": "could not parse app/legacy.py"}]}
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: _fake_completed(stdout=json.dumps(payload))
    )

    result = run_semgrep(tmp_path)
    assert result.findings == []
    assert result.scan_errors == ["could not parse app/legacy.py"]


def test_missing_binary_raises_semgrep_unavailable(monkeypatch, tmp_path: Path) -> None:
    def _raise(*a, **k):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr("subprocess.run", _raise)
    with pytest.raises(SemgrepUnavailable):
        run_semgrep(tmp_path)


def test_timeout_raises_semgrep_error(monkeypatch, tmp_path: Path) -> None:
    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd="semgrep", timeout=1.0)

    monkeypatch.setattr("subprocess.run", _raise)
    with pytest.raises(SemgrepError):
        run_semgrep(tmp_path, timeout_seconds=1.0)


def test_empty_stdout_raises_semgrep_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: _fake_completed(stdout="", stderr="boom", returncode=2)
    )
    with pytest.raises(SemgrepError, match="boom"):
        run_semgrep(tmp_path)


def test_invalid_json_raises_semgrep_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _fake_completed(stdout="not json"))
    with pytest.raises(SemgrepError):
        run_semgrep(tmp_path)


def test_non_directory_raises_semgrep_error(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    with pytest.raises(SemgrepError):
        run_semgrep(missing)
