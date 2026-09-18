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

Phase 1 (API quality loop), the dashboard, `compose` mode, static route parsing,
a first browser E2E layer, an Explorer Agent — link-crawl and interactive
(form filling, clicking, inferred state transitions) — self-healing selector
proposals, and issue tracker sync (GitHub and Jira) are implemented and
measured. Every item from the original CLAUDE.md roadmap (phases 1-5, minus
multi-tenant infra) now has a working, tested implementation — see
[Roadmap](#roadmap).

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
CLI, the eval harness, 48 unit tests and lint — all run green without a database. The
dashboard was rendered against real pipeline output through the documented API
contract: all three pages, the setup state, and the failure-analysis chart.

**Implemented but not yet exercised end to end:** the Postgres-backed paths — schema
creation, RLS policies, the worker and persistence. They import cleanly and the SQL is
in `db_init.py`, but confirming them needs a working Docker daemon.

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
qagent scan --url ... --repo ./path/to/checkout  # fall back to route parsing if no OpenAPI doc
```

`qagent scan` exits non-zero when a defect is found, so it drops straight into CI.

### Compose mode

For a repository that ships its own `docker-compose.yml` (ADR-0001), QAgent can
own the whole lifecycle instead of requiring an already-running target:

```bash
qagent scan-repo --compose ./docker-compose.yml --service api --port 8080
```

This brings the stack up (`docker compose up -d --build --wait`), polls the named
service until it answers with a non-5xx status, runs the identical pipeline used
by `qagent scan`, and tears the stack down (`docker compose down -v`) whether the
run succeeded, failed, or the pipeline itself raised. No new code path: the same
`run_pipeline` function backs both commands, so this is provisioning bolted onto
the existing loop, not a second loop to keep in sync.

### Browser E2E

For the frontend, a first check that needs an actual browser rather than an HTTP
client — no clicking, no generated selectors (that's the Explorer Agent, later):

```bash
pip install -e ./apps/api[e2e] && playwright install chromium

qagent scan-ui --url http://localhost:3000 --route /login --route /dashboard
```

Each page is loaded and flagged if it responds with a 5xx, throws an uncaught JS
exception, or logs a console error. That's still unambiguous evidence of a
defect — there's no selector here to go stale — so it doesn't reintroduce the
flake ADR-0002 kept out of the API layer.

### Explorer agent

`--route` requires the operator to already know every page. The explorer finds
the ones nobody listed by crawling same-origin `<a href>` links breadth-first
and building a state graph (CLAUDE.md §8-9):

```bash
qagent explore --url http://localhost:3000 --max-pages 25 --check
```

`--check` feeds every discovered page straight into the same page-load checks
`scan-ui` runs, so one command answers both "what does this app actually have"
and "does any of it throw."

The same crawl-then-check stage runs inside `run_pipeline` itself when an
environment has `e2e_enabled: true` (set on `POST .../environments`), which is
what makes it part of a persisted `Run QA` rather than a CLI-only side tool: a
failed page check joins the same `test_results`/`bugs` tables as an API result,
so it shows up in the dashboard and the quality gate exactly like one. It
degrades to a recorded, non-fatal skip when Playwright isn't installed on the
worker, rather than failing the whole run.

### Interactive exploration

Add `--interact` to fill forms and click through pages instead of only
following links — the "choose an action, observe the result, update the state
graph" loop CLAUDE.md §8-9 describes (ADR-0005):

```bash
qagent explore --url http://localhost:3000 --interact --max-actions 40
```

Every actionable element on a page is extracted and ranked by a deterministic
heuristic (testid, call-to-action text, form membership, required-ness,
position) — no model needed to produce a useful result. A model is consulted
only when the ranking is genuinely ambiguous, and even then it can only choose
an index into the already-ranked candidates, never invent a selector or an
action (ADR-0004 point 3). Required fields are filled before their own form's
submit button fires — scoped per form, so one empty field never blocks a
*different* form elsewhere on the page — and forms are only ever filled with
synthetic values.

A page offering several independent things worth trying (two separate forms,
a form plus an unrelated button) doesn't lose the others the moment one of
them is chosen: each top-level candidate gets its own reload-and-try attempt,
so the explorer covers what the page actually offers rather than only ever
walking the single highest-ranked path. A `confirm()`/`alert()` triggered by
any action is auto-dismissed rather than left to hang the crawl.

State identity is `(path, structural fingerprint)`, not bare URL, so an action
that changes the page without navigating — a form submitting into a "thanks"
state, a modal opening — is recorded as a distinct node instead of being
silently missed or looped on forever.

Destructive-looking actions (delete, pay, cancel, checkout, ...) are skipped
by default; pass `--allow-destructive` to permit them. A failed *action* (an
element the heuristic couldn't find or click) is never reported as a defect —
that's evidence about the selector, not the application, the same reasoning
ADR-0002 applies to a UI assertion. An uncaught exception or console error
triggered *by* an action is unambiguous evidence of a defect regardless of
which element caused it, and is reported the same way a failed page-load check
is. `--json` writes the full state graph, including every action taken and its
outcome.

Like `--check`, this runs inside `run_pipeline` when an environment has
`interactive_exploration_enabled: true`, folding any defect found into the
same `test_results`/`bugs` tables as `kind="e2e_interactive"`.

### Self-healing selectors

When a selector a test relies on stops matching, QAgent proposes a replacement
by comparing what the old selector *meant* (its id, `data-testid`, label, text)
against every element still on the page — never by DOM position, and never by
rewriting the test itself (CLAUDE.md §11):

```bash
qagent heal-selectors --url http://localhost:3000/checkout \
  --selector 'button[data-testid="checkout"]'
```

Below the confidence threshold (0.7 by default) it reports "no confident
match" rather than a guess — a wrong high-confidence proposal is worse than an
honest failure, since a human reviews either way. Every proposal is printed for
manual approval; nothing here ever touches a test file.

### Security scanning

Static analysis, not a live-target scanner (CLAUDE.md §16) — QAgent shells out
to Semgrep rather than reimplementing SAST rule coverage:

```bash
pip install -e ./apps/api[security]   # or a system semgrep install

qagent security --repo . --fail-on high
```

Semgrep's own ERROR/WARNING/INFO severities map to
critical/high/medium/low/info; a SQL-injection- or access-control-shaped
finding (OWASP A01/A03) is promoted to critical regardless of Semgrep's label,
since those are the two classes CLAUDE.md calls out by name. `--fail-on` sets
the CI gate: exit non-zero at or above that severity. Because this is static
analysis - it parses source, it never executes it - none of the sandboxing
section 22 requires for the runner/browser/explorer stages applies here, so it
also runs synchronously via `POST /api/v1/projects/{id}/security/scan` (given
a `repo_path` readable by the API process), persisting findings the same way a
bug does: they show up in the dashboard and count toward the quality gate.

### Performance testing

Load testing against a live target via k6 (CLAUDE.md §17):

```bash
# k6 is a standalone Go binary, not a Python package — install it separately:
# https://grafana.com/docs/k6/latest/set-up/install-k6/

qagent perf --url http://localhost:8080 --vus 100,500,1000,5000 --duration 30
```

Runs one scenario per VU level, sequentially — so degradation as load rises is
visible scenario-to-scenario rather than confounded by several scenarios
hitting the same target at once. Only GET requests against `--path` (default
`/`) are ever sent, never a discovered POST/PUT/DELETE endpoint: guessing at a
destructive path under sustained concurrent load needs a human to opt in.
Measures latency, throughput and error rate from the client side; CPU/memory
(also named in CLAUDE.md §17) need an agent on the target host, which a load
*generator* has no way to provide, so they're out of scope by necessity, not
oversight. `--max-failed-rate`/`--max-p95-ms` set the CI gate per scenario.

Unlike the security scan, this sends real sustained traffic to a live target
for potentially minutes, so `POST /api/v1/projects/{id}/performance/scan`
queues it on the same Celery worker that runs a regular scan rather than
running inline. Scenarios from one load test share a single database
transaction (and therefore one `created_at`), which is what the quality gate
uses to mean "the latest load test" — a load test failing once in the past
does not block every deploy after it, only its own latest run.

### Issue tracker sync

Turn a scan's defects into tracked GitHub issues instead of leaving them in a
JSON file:

```bash
qagent scan --url http://127.0.0.1:8080 --json out.json
qagent report-issues --json out.json --repo your-org/your-repo --label bug
```

Additive only: it never edits, closes, or comments on an issue a human already
owns, and a bug already filed (matched by its title) is skipped, never
duplicated, on the next run. `--dry-run` shows what would be filed without a
token or a network call.

The same contract ships for Jira:

```bash
qagent report-issues-jira --json out.json \
  --base-url https://your-org.atlassian.net --project QA
```

Full stack (Postgres, Redis, API, worker):

```bash
cp .env.example .env
docker compose up --build
```

### Dashboard

```bash
cd apps/web
npm install
cp .env.example .env.local     # set QAGENT_API_URL
npm run dev                    # http://localhost:3000
```

Visiting it redirects to `/login`, where you can create an organization (or sign
in to an existing one). The dashboard authenticates against the API with a
bearer token issued by `POST /api/v1/auth/login`, stored as an httpOnly cookie -
there is no `QAGENT_ORG_ID` to configure any more.

It reads the API and shows the pass rate, open defects by severity, the quality
gate decision, per-run failure analysis and full defect reports. Signed out or
unreachable, it says exactly what is wrong rather than rendering a shell of
zeroes - a dashboard reporting 0 defects because it cannot connect is worse than
no dashboard.

The failure-analysis chart is an **emphasis** design, not a categorical one: the
reader's question is "how many of these are actually my fault", so `real_bug`
carries the accent and every other class is de-emphasis gray. Spending eight
hues there would make the answer harder to see. Every bar is direct-labelled and
the same numbers are available as a table, so identity never rests on color.

---

## How it works

### 1. Discover

An OpenAPI document is fetched from an explicit URL or probed at seven common
locations, then flattened into endpoints with local `$ref`s resolved. Every endpoint
gets a **risk score** from its method, path and security declaration, and generation
works down that ranking — so a truncated budget still covers what matters.

When no document can be found and `--repo` (or `scan-repo`'s compose checkout) is
given, discovery falls back to **static route parsing**: an AST walk over Python
source for FastAPI/Flask-style decorators (`@app.get("/x")`,
`@app.route("/x", methods=[...])`) and a scan of JS/TS source for Express-style
calls (`router.post("/x", ...)`). This is deliberately narrow — only literal route
strings in a recognised call shape are found, never a dynamically built path or a
route table loaded from config — but recovering *some* of the surface beats
requiring every project to publish an OpenAPI document before QAgent is useful.

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
| [0005](docs/decisions/ADR-0005-interactive-exploration.md) | Interactive explorer state identity is `(path, structural fingerprint)`; actions are a closed schema-constrained set, ranked rules-first. |

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
│   ├── discovery/         OpenAPI ingestion, static route parsing, risk scoring
│   ├── generator/         deterministic rules + value synthesis
│   ├── runner/            SSRF-guarded execution, assertions
│   ├── triage/            failure classification, bug reports
│   ├── provisioning/      compose mode: stack lifecycle, health wait
│   ├── browser/           Playwright page-load checks + selector healing (`e2e` extra)
│   ├── explorer/          BFS link-crawl + interactive exploration, application state graph
│   ├── security/          Semgrep SAST wrapper (`security` extra)
│   ├── performance/       k6 load-test wrapper (standalone binary, no extra)
│   ├── integrations/      GitHub Issues + Jira sync
│   └── llm/               providers, budgets, safety boundary
├── eval/harness.py        scores the pipeline against ground truth
└── worker/tasks.py        Celery
apps/web/                  Next.js dashboard (server components, no client fetching)
packages/fixtures/         apps with labelled, seeded defects
docs/decisions/            ADRs
```

## Tests

```bash
cd apps/api && pytest tests -q     # 204 tests
```

CI runs lint, unit tests, **and the evaluation harness** — a change that degrades
detection or raises false positives fails the build.

---

## Roadmap

Phase 1, the dashboard, `compose` mode, route parsing, a first browser E2E
layer, the Explorer Agent (link-crawl and interactive), self-healing selector
proposals, and GitHub + Jira issue sync are done. Every item on the original
roadmap (CLAUDE.md phases 1-5, minus multi-tenant infra) now has a working,
tested implementation.

What's left is mostly Phase 6 (CLAUDE.md §23): multi-tenancy hardening beyond
the RLS already in place, distributed workers, and the operational surface
(rate limiting, billing/usage, broader observability) that only matters once
there's load to justify it — see ADR-0001 on resisting premature architecture.

More fixtures are the highest-leverage work at any point: every metric above is only
as trustworthy as the ground truth behind it.
