# QAgent

**Autonomous AI software quality engineering platform.**

QAgent points itself at a running application, discovers its API surface, generates
and executes checks against it, and then does the part that actually matters:
it decides *why* each failure happened and reports only the ones that are real defects.

```
Discover  →  Generate  →  Execute  →  Triage  →  Report
```

---

## Status

Phase 1 (API quality loop) is implemented and measured. The browser/E2E layer, the
dashboard and repository provisioning are not yet built — see [Roadmap](#roadmap).

Current measured performance against the reference fixture:

| Metric | Value |
|---|---|
| Detection rate | 100% (4 of 4 seeded defects) |
| **False positive rate** | **0%** |
| Triage accuracy | 100% |
| Runtime | 18 checks in ~1.2s |
| Cost | $0.00 (rules-only path) |

Reproduce these numbers yourself with the [two commands below](#try-it).

**Verified:** the pipeline (discovery, generation, execution, triage, reporting), the
CLI, the eval harness, 44 unit tests and lint — all run green without a database.

**Implemented but not yet exercised end to end:** the Postgres-backed paths — schema
creation, RLS policies, the worker and persistence. They import cleanly and the SQL is
in `db_init.py`, but they need `docker compose up` to confirm.

---

## Why triage is the product

Anyone can generate tests. The reason automated QA tools get switched off is that
they report noise: a red test is not a defect, and roughly half of them are the
suite's own fault.

QAgent classifies every failure before reporting it:

```
FAILURE
├── real_bug        an actual application defect
├── flaky_test      passed and failed recently without a change
├── environment     unreachable, or credentials not configured
├── network         timed out before a verdict
├── dependency      a database or upstream service was down
├── test_data       the fixture this case needs is missing
├── bad_assertion   the app was right, the test was over-specified
└── unknown         escalated for review
```

Only `real_bug` becomes a defect report, and only `real_bug` blocks a deployment.
A quality gate that blocks because staging was down is a quality gate that gets
disabled permanently.

The classifier is **rules-first**: deterministic, free, instant, and therefore
measurable. A model is consulted only where the rules are genuinely uncertain
(confidence < 0.70), and its answer is recorded separately so the two can be compared.
**Everything works with no API key configured.**

---

## Try it

Requires Python 3.11+.

```bash
python -m venv .venv && .venv/Scripts/activate      # Linux/macOS: source .venv/bin/activate
pip install -e ./apps/api "uvicorn[standard]"

# 1. Start the fixture app (it has four deliberately seeded defects)
cd packages/fixtures/buggy-shop && uvicorn app:app --port 8080 &

# 2. Scan it
qagent scan --url http://127.0.0.1:8080
```

You should see QAgent discover 8 endpoints, run 18 checks, find the seeded defects,
and correctly decline to report the endpoints that are merely unauthenticated.

Score itself against the ground truth:

```bash
qagent evaluate --url http://127.0.0.1:8080 --fixtures packages/fixtures
```

Other commands:

```bash
qagent endpoints --url http://127.0.0.1:8080     # discovered surface, ranked by risk
qagent scan --url ... --json out.json            # machine-readable results
qagent scan --url ... -H "Authorization: Bearer $TOKEN"
```

`qagent scan` exits non-zero when a defect is found, so it drops straight into CI.

Full stack (Postgres, Redis, API, worker):

```bash
cp .env.example .env
docker compose up --build
```

---

## How it works

### 1. Discover

An OpenAPI document is fetched from an explicit URL or probed at seven common
locations, then flattened into endpoints with local `$ref`s resolved. Every endpoint
gets a **risk score** from its method, path and security declaration, and generation
works down that ranking — so a truncated budget still covers what matters.

### 2. Generate

Seven deterministic rules per endpoint, including the ones that find real defects
most reliably:

- omit a required field → must be 4xx, never 5xx
- send a wrongly typed field → must be 4xx, never 5xx
- send a malformed path identifier → must be 400/404, never 5xx
- reference a well-formed but absent resource → must be 404
- strip credentials from a protected endpoint → must be 401/403
- present an `alg=none` token → must be 401/403

Generated cases are **declarative documents, not emitted code**: diffable, reviewable,
and safe to execute without `eval`. The same spec can be rendered to Playwright or
pytest later without regenerating anything.

### 3. Execute

An SSRF-guarded HTTP runner. Assertions are data, never expressions, so a generated
or model-suggested test can never execute arbitrary logic inside the platform.

### 4. Triage & report

Rules classify, the model arbitrates ambiguity, and only real defects become bug
reports with reproduction steps, expected vs. actual, root cause and a suggested fix.

---

## Architecture

One FastAPI service, one worker, Postgres, Redis.

```
CLI ─┐
     ├─→ API ─→ Redis queue ─→ Worker ─→ pipeline ─→ Postgres
CI ──┘                                       │
                                             └─→ discover → generate → execute → triage
```

The module boundaries from [reference image 03](docs/images/03-layered-modules.png)
are real; the twelve microservices it draws are not. Splitting them across processes
before there is load to justify it buys distributed-systems failure modes and nothing
else. The seams are in place for when that changes.

The pipeline itself (`qagent/pipeline.py`) imports neither the database nor Celery.
The CLI, the worker and the eval harness all run the identical loop — which means
**the thing the evaluation suite measures is exactly the thing that runs in production**.

### Key decisions

| ADR | Decision |
|---|---|
| [0001](docs/decisions/ADR-0001-scope-and-input-contract.md) | Two input contracts only: a running URL, or a compose file. Nothing provisions arbitrary repos. |
| [0002](docs/decisions/ADR-0002-api-tests-before-e2e.md) | API tests ship before browser E2E — a generated selector failure is indistinguishable from a real defect. |
| [0003](docs/decisions/ADR-0003-evaluation-harness.md) | The eval harness is a first-class component; false-positive rate is the primary metric. |
| [0004](docs/decisions/ADR-0004-untrusted-content-boundary.md) | Repo and response content is untrusted input. Verdicts come only from constrained schemas. |

---

## Security

QAgent reads attacker-controlled content and reaches out to user-supplied addresses,
so both are treated as hostile:

- **SSRF guard.** Targets resolving to link-local (`169.254.0.0/16` — cloud metadata),
  loopback or private ranges are rejected outside local development, with an optional
  host allowlist. Re-checked per request, not just per run.
- **Prompt injection.** Third-party content is fenced and labelled untrusted, and the
  fence cannot be closed early by a hostile payload. Verdict fields (pass/fail,
  severity, classification) are reachable *only* through a constrained JSON schema, so
  model prose can never set one.
- **Secret scrubbing.** Bearer tokens, API keys, JWTs and database DSNs are redacted
  before anything is persisted as an artifact or sent to a provider.
- **Tenant isolation.** `org_id` on every table with Postgres row-level security
  (`USING` *and* `WITH CHECK`, plus `FORCE`), so isolation cannot be forgotten at a
  call site.
- **Budgets.** Per-run caps on calls, tokens and spend. Exceeding one degrades to the
  rule-based path rather than failing the run.

The worker container runs read-only, with `no-new-privileges`, no exposed ports, and
no Docker socket.

> Docker is not a security boundary against genuinely hostile code. Before `compose`
> mode executes third-party repositories, runners need gVisor or Firecracker and a
> default-deny egress proxy.

---

## Repository layout

```
apps/api/qagent/
├── pipeline.py            the loop; no database, no queue
├── main.py                HTTP API
├── models.py              schema, org_id + RLS on every tenant table
├── cli.py                 qagent scan | endpoints | evaluate
├── modules/
│   ├── discovery/         OpenAPI ingestion, risk scoring
│   ├── generator/         deterministic rules + value synthesis
│   ├── runner/            SSRF-guarded execution, assertions
│   ├── triage/            failure classification, bug reports
│   └── llm/               providers, budgets, safety boundary
├── eval/harness.py        scores the pipeline against ground truth
└── worker/tasks.py        Celery
packages/fixtures/         apps with labelled, seeded defects
docs/decisions/            ADRs
```

## Tests

```bash
cd apps/api && pytest tests -q     # 44 tests
```

CI runs lint, unit tests, **and the evaluation harness** — a change that degrades
detection or raises false positives fails the build.

---

## Roadmap

Phase 1 is done. In order:

1. **Dashboard** — Next.js UI over the existing API.
2. **`compose` mode** — bring a repository's stack up, seed, run, tear down (ADR-0001).
3. **Route parsing** — discover endpoints in projects with no OpenAPI document.
4. **Browser E2E** — Playwright, once API triage is trustworthy (ADR-0002).
5. **Explorer agent** — state graph, autonomous exploration.
6. **Self-healing selectors** — proposal and approval flow, never silent rewrites.
7. **Issue tracker sync** — defect loop out to Jira / GitHub Issues.

More fixtures are the highest-leverage work at any point: every metric above is only
as trustworthy as the ground truth behind it.
