"""k6 wrapper: parsing and scenario orchestration tested against a fake
subprocess, so these tests need neither the ``k6`` binary nor a live target.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from qagent.modules.performance.k6 import (
    K6Error,
    K6Unavailable,
    PerformanceMetrics,
    run_k6,
    run_load_test,
)

SUMMARY = {
    "metrics": {
        "http_reqs": {"values": {"count": 1200, "rate": 40.0}},
        "http_req_failed": {"values": {"rate": 0.01}},
        "http_req_duration": {
            "values": {"avg": 120.5, "p(95)": 300.0, "p(99)": 450.0, "max": 900.0}
        },
    }
}


def _fake_completed(returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(args=["k6"], returncode=returncode, stdout="", stderr=stderr)


def _install_fake_k6(monkeypatch: pytest.MonkeyPatch, summary: dict | None = SUMMARY) -> None:
    def fake_run(args, **kwargs):
        # --summary-export is followed by the path k6 would write to.
        export_path = args[args.index("--summary-export") + 1]
        if summary is not None:
            with open(export_path, "w", encoding="utf-8") as f:
                json.dump(summary, f)
        return _fake_completed()

    monkeypatch.setattr("subprocess.run", fake_run)


def test_parses_summary_into_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_k6(monkeypatch)

    metrics = run_k6("http://example.test", vus=100, duration_seconds=30.0)
    assert metrics == PerformanceMetrics(
        vus=100,
        duration_s=30.0,
        requests=1200,
        requests_per_s=40.0,
        failed_rate=0.01,
        latency_avg_ms=120.5,
        latency_p95_ms=300.0,
        latency_p99_ms=450.0,
        latency_max_ms=900.0,
    )


def test_passes_within_thresholds() -> None:
    metrics = PerformanceMetrics(
        vus=100,
        duration_s=30,
        requests=100,
        requests_per_s=3.3,
        failed_rate=0.0,
        latency_avg_ms=50,
        latency_p95_ms=200,
        latency_p99_ms=300,
        latency_max_ms=400,
    )
    assert metrics.passes(max_failed_rate=0.01, max_p95_ms=500)


def test_fails_on_high_error_rate() -> None:
    metrics = PerformanceMetrics(
        vus=100,
        duration_s=30,
        requests=100,
        requests_per_s=3.3,
        failed_rate=0.05,
        latency_avg_ms=50,
        latency_p95_ms=200,
        latency_p99_ms=300,
        latency_max_ms=400,
    )
    assert not metrics.passes(max_failed_rate=0.01, max_p95_ms=500)


def test_fails_on_slow_p95() -> None:
    metrics = PerformanceMetrics(
        vus=100,
        duration_s=30,
        requests=100,
        requests_per_s=3.3,
        failed_rate=0.0,
        latency_avg_ms=50,
        latency_p95_ms=900,
        latency_p99_ms=1200,
        latency_max_ms=1500,
    )
    assert not metrics.passes(max_failed_rate=0.01, max_p95_ms=500)


def test_passes_base_url_and_paths_via_env_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        export_path = args[args.index("--summary-export") + 1]
        with open(export_path, "w", encoding="utf-8") as f:
            json.dump(SUMMARY, f)
        return _fake_completed()

    monkeypatch.setattr("subprocess.run", fake_run)

    run_k6("http://example.test", vus=50, duration_seconds=10, paths=["/health", "/products"])

    args = captured["args"]
    assert "QAGENT_BASE_URL=http://example.test" in args
    assert 'QAGENT_PATHS=["/health", "/products"]' in args
    assert "--vus" in args and args[args.index("--vus") + 1] == "50"
    assert "--duration" in args and args[args.index("--duration") + 1] == "10s"


def test_missing_binary_raises_k6_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*a, **k):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr("subprocess.run", _raise)
    with pytest.raises(K6Unavailable):
        run_k6("http://example.test", vus=10)


def test_timeout_raises_k6_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd="k6", timeout=1.0)

    monkeypatch.setattr("subprocess.run", _raise)
    with pytest.raises(K6Error):
        run_k6("http://example.test", vus=10, timeout_seconds=1.0)


def test_missing_summary_raises_k6_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_k6(monkeypatch, summary=None)
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: _fake_completed(returncode=1, stderr="boom")
    )
    with pytest.raises(K6Error, match="boom"):
        run_k6("http://example.test", vus=10)


def test_invalid_summary_json_raises_k6_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args, **kwargs):
        export_path = args[args.index("--summary-export") + 1]
        with open(export_path, "w", encoding="utf-8") as f:
            f.write("not json")
        return _fake_completed()

    monkeypatch.setattr("subprocess.run", fake_run)
    with pytest.raises(K6Error):
        run_k6("http://example.test", vus=10)


def test_run_load_test_runs_one_scenario_per_vu_level(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_k6(monkeypatch)

    result = run_load_test("http://example.test", vus_levels=[100, 500], duration_seconds=5)
    assert [s.vus for s in result.scenarios] == [100, 500]
    assert all(s.requests == 1200 for s in result.scenarios)


def test_run_load_test_defaults_to_claude_md_levels(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_k6(monkeypatch)

    result = run_load_test("http://example.test", duration_seconds=5)
    assert [s.vus for s in result.scenarios] == [100, 500, 1000, 5000]
