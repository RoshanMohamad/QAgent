"""Load testing via Apache JMeter (CLAUDE.md section 17).

k6 is the better default - a single Go binary, JSON summary, no XML - and it
stays the default. JMeter is here because CLAUDE.md names it and because a lot
of organisations already have JMeter expertise, existing test plans, and a
CI job that knows how to run one. Making them abandon that to use QAgent would
be the wrong trade.

Both tools report into `PerformanceMetrics`, the same shape k6 already
produces, so a threshold, a dashboard row and a quality gate cannot tell which
generator ran. That is the entire point of the integration layer.

**JMeter reports differently, and the difference is not cosmetic.** k6 hands
back pre-aggregated metrics; JMeter writes a per-sample CSV and expects the
reader to aggregate. So this module computes the percentiles itself, and the
method is stated rather than assumed: nearest-rank on the sorted sample list,
which is what JMeter's own HTML report uses. Two tools disagreeing about what
"p95" means while reporting into one column would be a silent lie.
"""

from __future__ import annotations

import csv
import logging
import math
import subprocess
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from qagent.modules.performance.k6 import PerformanceMetrics

logger = logging.getLogger(__name__)


class JMeterUnavailable(RuntimeError):
    """JMeter is not installed or not on PATH."""


class JMeterError(RuntimeError):
    """JMeter ran but failed for a reason unrelated to the results."""


#: A minimal JMX test plan. Values are XML-escaped before substitution rather
#: than trusted: `base_url` and the paths come from a caller, and a JMX is XML,
#: so an unescaped `&` alone would produce a file JMeter refuses to parse -
#: never mind a deliberately crafted one closing a tag early.
_PLAN = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="QAgent" enabled="true">
      <boolProp name="TestPlan.functional_mode">false</boolProp>
      <elementProp name="TestPlan.user_defined_variables" elementType="Arguments">
        <collectionProp name="Arguments.arguments"/>
      </elementProp>
    </TestPlan>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="Load"
                   enabled="true">
        <stringProp name="ThreadGroup.num_threads">{vus}</stringProp>
        <stringProp name="ThreadGroup.ramp_time">{ramp}</stringProp>
        <boolProp name="ThreadGroup.scheduler">true</boolProp>
        <stringProp name="ThreadGroup.duration">{duration}</stringProp>
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController">
          <boolProp name="LoopController.continue_forever">true</boolProp>
          <stringProp name="LoopController.loops">-1</stringProp>
        </elementProp>
      </ThreadGroup>
      <hashTree>
{samplers}
      </hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
"""

_SAMPLER = """        <HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy"
                          testname="{name}" enabled="true">
          <stringProp name="HTTPSampler.domain">{host}</stringProp>
          <stringProp name="HTTPSampler.port">{port}</stringProp>
          <stringProp name="HTTPSampler.protocol">{scheme}</stringProp>
          <stringProp name="HTTPSampler.path">{path}</stringProp>
          <stringProp name="HTTPSampler.method">GET</stringProp>
          <boolProp name="HTTPSampler.follow_redirects">true</boolProp>
        </HTTPSamplerProxy>
        <hashTree/>
"""


def build_plan(base_url: str, *, vus: int, duration_seconds: float, paths: list[str]) -> str:
    """Render a JMX test plan.

    GET-only, exactly like the k6 script next door and for the same reason:
    guessing at a destructive path under sustained concurrent load is not
    something to do without a human opting in.
    """
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if scheme == "https" else 80)

    samplers = "".join(
        _SAMPLER.format(
            name=escape(f"GET {path}"),
            host=escape(host),
            port=port,
            scheme=escape(scheme),
            path=escape(path),
        )
        for path in (paths or ["/"])
    )

    return _PLAN.format(
        vus=int(vus),
        # A ramp of a tenth of the run, capped: starting 5,000 threads
        # simultaneously measures the load generator, not the application.
        ramp=max(1, min(30, int(duration_seconds / 10))),
        duration=int(duration_seconds),
        samplers=samplers,
    )


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Nearest-rank percentile, matching JMeter's own HTML report.

    Stated explicitly because k6 and JMeter report into the same column: two
    tools quietly disagreeing about what "p95" means would make the number
    incomparable across runs without anything looking wrong.
    """
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil(fraction * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def parse_results(csv_text: str, *, vus: int, duration_s: float) -> PerformanceMetrics:
    """Aggregate a JMeter per-sample CSV into the shared metric shape."""
    latencies: list[float] = []
    failures = 0
    total = 0

    for row in csv.DictReader(csv_text.splitlines()):
        try:
            latencies.append(float(row.get("elapsed") or 0.0))
        except ValueError:
            continue
        total += 1
        # JMeter writes the literal strings "true"/"false"; anything else is a
        # malformed row and is counted as a failure rather than silently passed.
        if str(row.get("success", "")).strip().lower() != "true":
            failures += 1

    latencies.sort()
    return PerformanceMetrics(
        vus=vus,
        duration_s=duration_s,
        requests=total,
        requests_per_s=(total / duration_s) if duration_s else 0.0,
        failed_rate=(failures / total) if total else 0.0,
        latency_avg_ms=(sum(latencies) / len(latencies)) if latencies else 0.0,
        latency_p95_ms=_percentile(latencies, 0.95),
        latency_p99_ms=_percentile(latencies, 0.99),
        latency_max_ms=latencies[-1] if latencies else 0.0,
    )


def run_jmeter(
    base_url: str,
    *,
    vus: int,
    duration_seconds: float = 30.0,
    paths: list[str] | None = None,
    timeout_seconds: float = 900.0,
) -> PerformanceMetrics:
    """Run one JMeter scenario and return its summary metrics.

    Requires a `jmeter` binary on PATH. Like k6 it is not a Python package, so
    there is no pip extra for it.
    """
    with tempfile.TemporaryDirectory() as tmp:
        plan_path = Path(tmp) / "plan.jmx"
        results_path = Path(tmp) / "results.csv"
        plan_path.write_text(
            build_plan(
                base_url, vus=vus, duration_seconds=duration_seconds, paths=paths or ["/"]
            ),
            encoding="utf-8",
        )

        argv = [
            "jmeter",
            "-n",  # non-GUI; the GUI is for authoring, never for load
            "-t",
            str(plan_path),
            "-l",
            str(results_path),
            "-Jjmeter.save.saveservice.output_format=csv",
        ]

        try:
            # Fixed argv, no shell; PATH lookup of `jmeter` is intended.
            completed = subprocess.run(  # noqa: S603
                argv, capture_output=True, text=True, timeout=timeout_seconds, check=False
            )
        except FileNotFoundError as exc:
            raise JMeterUnavailable(
                "jmeter is not installed or not on PATH - see "
                "https://jmeter.apache.org/download_jmeter.cgi"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise JMeterError(f"jmeter timed out after {timeout_seconds}s") from exc

        if not results_path.exists():
            raise JMeterError(
                completed.stderr.strip()
                or f"jmeter exited {completed.returncode} without writing results"
            )

        metrics = parse_results(
            results_path.read_text(encoding="utf-8", errors="replace"),
            vus=vus,
            duration_s=duration_seconds,
        )

    logger.info(
        "jmeter: %d vus, %d requests, %.0fms p95",
        vus,
        metrics.requests,
        metrics.latency_p95_ms,
    )
    return metrics
