"""Prometheus metrics (CLAUDE.md section 23, Phase 6).

A handful of counters and one histogram, not a tracing system: this answers
"is the API up and how loaded is it" and "is the product actually finding
defects," which is the pair of questions an operator needs before anything
fancier (distributed tracing, per-endpoint SLOs) is worth building. Scraped at
`GET /metrics` in the default Prometheus text exposition format - no pushgateway,
no separate collector process, because there's exactly one API process to scrape.

Every metric here is a module-level singleton, which is the normal
`prometheus_client` pattern: the client library's registry is itself a global,
so redefining a `Counter` on an already-registered name raises. Import this
module once per process (Python's own module cache makes that automatic) and
call `.inc()`/`.observe()` from wherever the event actually happens -
`main.py`'s request middleware for HTTP metrics, `persistence.py` for
run/defect counts, since that's the one place both are known.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

#: `path_template`, not the raw request path: "/api/v1/runs/{run_id}", never
#: "/api/v1/runs/3aae0eff-...". A label with one value per UUID ever requested
#: is an unbounded-cardinality metric, which is the single most common way to
#: quietly take down a Prometheus server.
HTTP_REQUESTS = Counter(
    "qagent_http_requests_total",
    "HTTP requests received, by method, route template and status code.",
    ["method", "path_template", "status"],
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "qagent_http_request_duration_seconds",
    "HTTP request duration in seconds, by method and route template.",
    ["method", "path_template"],
)

#: One increment per finished TestRun, labelled by its terminal status - the
#: same vocabulary models.RunStatus already uses, so a dashboard built on this
#: never needs a second mapping to reconcile against the database.
RUNS_TOTAL = Counter(
    "qagent_runs_total",
    "Test runs that reached a terminal state, by status.",
    ["status"],
)

#: One increment per persisted Bug, labelled by severity - CLAUDE.md's own
#: dashboard mock (section 4) leads with exactly this breakdown.
DEFECTS_TOTAL = Counter(
    "qagent_defects_total",
    "Defects (Bug rows) persisted, by severity.",
    ["severity"],
)

LLM_SPEND_USD_TOTAL = Counter(
    "qagent_llm_spend_usd_total",
    "Cumulative LLM spend in USD across every agent run.",
)
