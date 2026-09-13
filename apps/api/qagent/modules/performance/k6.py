"""Load testing via k6 (CLAUDE.md section 17).

Same posture as modules/security/semgrep.py: don't reimplement a load generator,
shell out to one that people maintain full time, and translate its output into
this platform's vocabulary. k6 measures latency, throughput and error rate from
the client side; CPU and memory (also named in CLAUDE.md section 17) require an
agent on the target host, which is out of scope here - a load *generator* has no
way to observe them, and reporting zeros would be worse than not reporting a
field at all. What's implemented is exactly what a client-side tool can measure.

Unlike Semgrep, k6 sends real traffic to a live target, so this is capable of
side effects. The generated script is GET-only against `base_url` and the paths
the caller supplies - it never runs a discovered POST/PUT/DELETE endpoint
without the caller explicitly listing it, since guessing at a destructive path
under sustained concurrent load is not something to do without a human deciding
to opt in.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: Static k6 script. Target and paths are passed as -e environment variables on
#: the command line, never templated into the script text, so there is no
#: injection surface in what's ultimately a string built from caller-supplied
#: values (a base URL, a list of paths).
_SCRIPT = """
import http from 'k6/http';
import { sleep } from 'k6';

const BASE_URL = __ENV.QAGENT_BASE_URL;
const PATHS = JSON.parse(__ENV.QAGENT_PATHS || '["/"]');

export default function () {
  const path = PATHS[Math.floor(Math.random() * PATHS.length)];
  http.get(`${BASE_URL}${path}`);
  sleep(1);
}
"""

#: Passed to --summary-trend-stats so p(99) is always present; k6's default
#: summary omits it.
_TREND_STATS = "avg,min,med,max,p(90),p(95),p(99)"


class K6Unavailable(RuntimeError):
    """k6 is not installed or not on PATH."""


class K6Error(RuntimeError):
    """k6 ran but exited with an error unrelated to a scenario's own thresholds."""


@dataclass
class PerformanceMetrics:
    vus: int
    duration_s: float
    requests: int
    requests_per_s: float
    failed_rate: float
    latency_avg_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    latency_max_ms: float

    def passes(self, *, max_failed_rate: float, max_p95_ms: float) -> bool:
        return self.failed_rate <= max_failed_rate and self.latency_p95_ms <= max_p95_ms


@dataclass
class LoadTestResult:
    base_url: str
    scenarios: list[PerformanceMetrics] = field(default_factory=list)


def _metric(metrics: dict, name: str, key: str, default: float = 0.0) -> float:
    return float(metrics.get(name, {}).get("values", {}).get(key, default))


def _parse_summary(summary: dict, *, vus: int, duration_s: float) -> PerformanceMetrics:
    metrics = summary.get("metrics", {})
    return PerformanceMetrics(
        vus=vus,
        duration_s=duration_s,
        requests=int(_metric(metrics, "http_reqs", "count")),
        requests_per_s=_metric(metrics, "http_reqs", "rate"),
        failed_rate=_metric(metrics, "http_req_failed", "rate"),
        latency_avg_ms=_metric(metrics, "http_req_duration", "avg"),
        latency_p95_ms=_metric(metrics, "http_req_duration", "p(95)"),
        latency_p99_ms=_metric(metrics, "http_req_duration", "p(99)"),
        latency_max_ms=_metric(metrics, "http_req_duration", "max"),
    )


def run_k6(
    base_url: str,
    *,
    vus: int,
    duration_seconds: float = 30.0,
    paths: list[str] | None = None,
    timeout_seconds: float = 600.0,
) -> PerformanceMetrics:
    """Run one k6 scenario at a fixed VU count and return its summary metrics.

    Requires a k6 binary on PATH - it's a standalone Go binary, not a Python
    package, so there is no pip extra for it (see the README for install links).
    """
    with tempfile.TemporaryDirectory() as tmp:
        script_path = Path(tmp) / "load_test.js"
        summary_path = Path(tmp) / "summary.json"
        script_path.write_text(_SCRIPT, encoding="utf-8")

        args = [
            "k6",
            "run",
            "--vus",
            str(vus),
            "--duration",
            f"{int(duration_seconds)}s",
            "--summary-trend-stats",
            _TREND_STATS,
            "--summary-export",
            str(summary_path),
            "--quiet",
            "-e",
            f"QAGENT_BASE_URL={base_url}",
            "-e",
            f"QAGENT_PATHS={json.dumps(paths or ['/'])}",
            str(script_path),
        ]

        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                args, capture_output=True, text=True, timeout=timeout_seconds, check=False
            )
        except FileNotFoundError as exc:
            raise K6Unavailable(
                "k6 is not installed or not on PATH (see README for install instructions)"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise K6Error(f"k6 timed out after {timeout_seconds}s") from exc

        if not summary_path.exists():
            raise K6Error(
                completed.stderr.strip() or f"k6 exited {completed.returncode} with no summary"
            )

        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise K6Error(f"could not parse k6 summary: {exc}") from exc

    metrics = _parse_summary(summary, vus=vus, duration_s=duration_seconds)
    logger.info(
        "k6 scenario vus=%d: %d req, %.1f req/s, %.2f%% failed, p95=%.0fms",
        vus,
        metrics.requests,
        metrics.requests_per_s,
        metrics.failed_rate * 100,
        metrics.latency_p95_ms,
    )
    return metrics


def run_load_test(
    base_url: str,
    *,
    vus_levels: list[int] | None = None,
    duration_seconds: float = 30.0,
    paths: list[str] | None = None,
    timeout_seconds: float = 600.0,
) -> LoadTestResult:
    """Run one scenario per VU level, ramping up (CLAUDE.md's 100/500/1000/5000 users).

    Scenarios run sequentially, not concurrently: the point is to see where the
    target starts degrading as load increases, which a stack of simultaneous
    scenarios against the same target would only confound.
    """
    result = LoadTestResult(base_url=base_url)
    for vus in vus_levels or [100, 500, 1000, 5000]:
        result.scenarios.append(
            run_k6(
                base_url,
                vus=vus,
                duration_seconds=duration_seconds,
                paths=paths,
                timeout_seconds=timeout_seconds,
            )
        )
    return result
