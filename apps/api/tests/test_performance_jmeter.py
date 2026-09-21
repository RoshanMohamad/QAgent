"""JMeter as a second load generator.

The tests that matter are the ones asserting JMeter and k6 are *interchangeable*
at the reporting layer. Two generators quietly disagreeing about what "p95"
means, while writing into the same column, would make the number incomparable
across runs without anything looking broken.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qagent.modules.performance.jmeter import (
    JMeterError,
    JMeterUnavailable,
    _percentile,
    build_plan,
    parse_results,
    run_jmeter,
)
from qagent.modules.performance.k6 import PerformanceMetrics

_CSV = """timeStamp,elapsed,label,responseCode,success,bytes
1700000000000,100,GET /,200,true,120
1700000000100,200,GET /,200,true,120
1700000000200,300,GET /,500,false,80
1700000000300,400,GET /,200,true,120
"""


# ----------------------------------------------------------------- the plan


def test_the_plan_is_well_formed_xml() -> None:
    """A JMX is XML; JMeter refuses to start on a malformed one."""
    from xml.etree import ElementTree

    plan = build_plan("http://shop.test:8080", vus=50, duration_seconds=30, paths=["/", "/cart"])

    # S314 is about parsing *untrusted* XML; this is the plan we just built,
    # and parsing it is the assertion - a malformed JMX is one JMeter refuses.
    root = ElementTree.fromstring(plan)  # noqa: S314
    assert root.tag == "jmeterTestPlan"


def test_the_target_is_split_into_jmeter_fields() -> None:
    plan = build_plan("https://shop.test", vus=10, duration_seconds=30, paths=["/cart"])

    assert "<stringProp name=\"HTTPSampler.domain\">shop.test</stringProp>" in plan
    assert "<stringProp name=\"HTTPSampler.protocol\">https</stringProp>" in plan
    # Default port for the scheme, not a literal "None".
    assert "<stringProp name=\"HTTPSampler.port\">443</stringProp>" in plan


def test_one_sampler_per_path() -> None:
    plan = build_plan("http://x", vus=1, duration_seconds=5, paths=["/a", "/b", "/c"])

    assert plan.count("HTTPSamplerProxy guiclass") == 3


def test_only_get_is_generated() -> None:
    """Guessing at a destructive path under sustained load is not something to
    do without a human opting in - same rule as the k6 script."""
    plan = build_plan("http://x", vus=1, duration_seconds=5, paths=["/orders"])

    assert plan.count("<stringProp name=\"HTTPSampler.method\">GET</stringProp>") == 1
    for verb in ("POST", "PUT", "DELETE", "PATCH"):
        assert f">{verb}<" not in plan


def test_xml_metacharacters_in_a_path_cannot_break_the_plan() -> None:
    from xml.etree import ElementTree

    plan = build_plan(
        "http://x", vus=1, duration_seconds=5, paths=['/a&b</stringProp><evil>']
    )

    # It still parses, which means the payload landed as text rather than markup.
    ElementTree.fromstring(plan)  # noqa: S314 - see above
    assert "<evil>" not in plan


def test_the_ramp_is_bounded() -> None:
    """Starting 5,000 threads at once measures the load generator."""
    plan = build_plan("http://x", vus=5000, duration_seconds=3600, paths=["/"])

    ramp = plan.split('name="ThreadGroup.ramp_time">')[1].split("<")[0]
    assert 1 <= int(ramp) <= 30


# -------------------------------------------------------------- aggregation


def test_samples_are_aggregated_into_the_shared_shape() -> None:
    metrics = parse_results(_CSV, vus=10, duration_s=4.0)

    assert isinstance(metrics, PerformanceMetrics)
    assert metrics.requests == 4
    assert metrics.latency_avg_ms == 250.0
    assert metrics.latency_max_ms == 400.0


def test_failures_are_counted_from_the_success_column() -> None:
    metrics = parse_results(_CSV, vus=10, duration_s=4.0)

    assert metrics.failed_rate == 0.25


def test_a_malformed_success_value_counts_as_a_failure() -> None:
    """Silently passing a row nobody can interpret would understate the error
    rate, which is the one number a load test exists to report."""
    csv_text = "timeStamp,elapsed,success\n1,100,\n1,100,true\n"

    assert parse_results(csv_text, vus=1, duration_s=1.0).failed_rate == 0.5


def test_throughput_is_derived_from_the_configured_duration() -> None:
    metrics = parse_results(_CSV, vus=10, duration_s=2.0)

    assert metrics.requests_per_s == 2.0


def test_an_empty_result_file_is_not_a_crash() -> None:
    metrics = parse_results("timeStamp,elapsed,success\n", vus=1, duration_s=10.0)

    assert metrics.requests == 0
    assert metrics.failed_rate == 0.0
    assert metrics.latency_p95_ms == 0.0


def test_unparseable_rows_are_skipped_not_fatal() -> None:
    csv_text = "timeStamp,elapsed,success\n1,not-a-number,true\n1,100,true\n"

    assert parse_results(csv_text, vus=1, duration_s=1.0).requests == 1


# -------------------------------------------------------------- percentiles


def test_percentiles_use_nearest_rank() -> None:
    """Stated explicitly because k6 reports into the same column."""
    values = [float(v) for v in range(1, 101)]

    assert _percentile(values, 0.95) == 95.0
    assert _percentile(values, 0.99) == 99.0
    assert _percentile(values, 1.0) == 100.0


def test_percentiles_on_a_single_sample() -> None:
    assert _percentile([42.0], 0.95) == 42.0


def test_percentiles_on_nothing() -> None:
    assert _percentile([], 0.95) == 0.0


def test_p95_is_never_above_the_maximum() -> None:
    metrics = parse_results(_CSV, vus=1, duration_s=1.0)

    assert metrics.latency_p95_ms <= metrics.latency_max_ms
    assert metrics.latency_p99_ms <= metrics.latency_max_ms


# --------------------------------------------------------------- unavailable


def test_a_missing_binary_says_how_to_get_one(monkeypatch, tmp_path: Path) -> None:
    def _missing(*args, **kwargs):
        raise FileNotFoundError("jmeter")

    monkeypatch.setattr("subprocess.run", _missing)

    with pytest.raises(JMeterUnavailable, match="jmeter.apache.org"):
        run_jmeter("http://x", vus=1, duration_seconds=1)


def test_a_run_that_wrote_no_results_is_an_error(monkeypatch) -> None:
    class _Completed:
        returncode = 1
        stdout = ""
        stderr = "Cannot access test plan"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Completed())

    with pytest.raises(JMeterError, match="Cannot access test plan"):
        run_jmeter("http://x", vus=1, duration_seconds=1)
