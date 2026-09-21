# QAgent

**Autonomous AI software quality engineering platform.**

QAgent points itself at a running application, discovers its API surface, generates
and executes checks against it, and then does the part that actually matters:
it decides *why* each failure happened and reports only the ones that are real defects.

```
Discover  →  Plan  →  Generate  →  Execute  →  Triage  →  Report
```

---

## Status

**Phases 1-5 are implemented, plus a scoped Phase 6.** Grouped rather than listed
as one sentence, because the list stopped being readable:

| Area | What exists |
|---|---|
| Core loop | Discovery (OpenAPI + static route parsing), Test Planner (agent 2), rule-based generation, SSRF-guarded execution, rules-first triage, defect reports with [HAR evidence](#evidence-on-every-defect) |
| Agents | Project Analyst (1), Test Planner (2), Generator + pytest emitter (3), Explorer — link-crawl and interactive (4), Bug Hunter / Failure Analyzer |
| Repository intelligence | Stack detection, module tree, [repository RAG](#repository-rag) — symbol-level chunking, BM25 + optional embeddings, pgvector when configured — feeding root-cause evidence into bug reports |
| Browser | E2E page checks, interactive exploration, self-healing selector proposals, [recorder extension](#browser-recorder) → Playwright spec |
| Security | Semgrep, Trivy and OWASP ZAP behind one severity vocabulary, plus a two-identity [IDOR probe](#broken-access-control-idor) |
| CI/CD | [Quality gate](#quality-gate-and-ci) + GitHub Action, recorded gate/deployment history, [automatic alerts](#defect-history-and-alerts) with delivery tracking and retry |
| Performance | k6 and Apache JMeter behind one metric shape |
| Deployment | [Validated Kubernetes manifests](#deployment), per-PR [ephemeral environments](#ephemeral-environments-per-pull-request), usage [priced into a statement](#billing) |
| Platform | Multi-tenancy with Postgres RLS, RBAC, rate limiting, `/metrics`, usage tracking, queue separation, [Alembic migrations](#schema-migrations), S3-compatible storage, OpenTelemetry spans |
| Dashboard | [Quality score](#quality-score), pass rate, defects and findings by severity, surface coverage, failure analysis, gate history |

See [Roadmap](#roadmap) for what is deliberately *not* built and what remains open.

Current measured performance against the reference fixture:

| Metric | Value |
|---|---|
| Detection rate | 100% (4 of 4 seeded defects) |
| **False positive rate** | **0%** |
| Triage accuracy | 100% |
| Runtime | 18 checks in ~1.2s |
| Cost | $0.00 (rules-only path) |

Reproduce these numbers yourself with the [two commands below](#try-it). A second
fixture, a different framework (Flask) with a deliberately *harder* defect — see
[Evaluating against a second fixture](#evaluating-against-a-second-fixture) —
now scores **6 of 6 at 0% false positives**. It read 5 of 6 for a long time: the
sixth is an IDOR, and no generator rule could reach it because every rule tests
one identity at a time. It is now found by a [probe](#broken-access-control-idor)
that compares two.

**Verified:** the pipeline (discovery, planning, generation, execution, triage,
reporting), the CLI, the eval harness, **543 unit tests** and lint — all run
green with no database and no API key. The dashboard was rendered against real
pipeline output through the documented API contract: all three pages, the setup
state, and the failure-analysis chart.

**Verified against a real Postgres and Redis** (`db-integration` in CI,
[ADR-0007](docs/decisions/ADR-0007-rls-requires-an-unprivileged-role.md)): schema
creation, row-level security, and the worker are no longer merely "imports cleanly."
Checking actually found a real bug — the API and worker connected as the same
Postgres superuser that bootstraps the schema, which silently bypasses RLS regardless
of `FORCE`, so every tenant could read and write every other tenant's rows. Fixed by
giving the app a separate, unprivileged role that `db_init` creates and strips of
`SUPERUSER`/`BYPASSRLS` on every run; `tests_integration/` now proves isolation
directly against that role and runs a real scan through a real out-of-process
`celery worker` end to end.

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

### Evaluating against a second fixture

`buggy-shop` alone can't tell you whether detection generalises or was fit to one
FastAPI app. `packages/fixtures/task-tracker` is a second fixture — Flask, a
hand-written OpenAPI document instead of a framework-generated one — with six
seeded defects instead of buggy-shop's four:

```bash
pip install flask   # or: pip install -e "./apps/api[dev]"
cd packages/fixtures/task-tracker && flask --app app run --port 8081 &

qagent evaluate --url http://127.0.0.1:8081 --name task-tracker --fixtures packages/fixtures
```

> **Restart the fixture between runs.** Generation deliberately produces
> destructive cases, and `DELETE /tasks/1` removes the row the IDOR probe uses
> as evidence — so a second `evaluate` against the same process reads 5/6
> instead of 6/6. The fixture holds its data in memory, so restarting it is the
> reset. CI starts a fresh one per job and is unaffected; this bites only when
> re-running locally.

Five of the six are the same categories the seven generator rules already catch
(missing/wrong-type fields, a malformed or absent identifier, unenforced auth),
seeded independently to show the rules generalise rather than being tuned to one
app. The sixth, BUG-201, is an IDOR — any authenticated user can read any other
user's task by id — and it passes every generated check, because each rule tests
one identity at a time.

It was recorded in `seeded_defects.yaml` as a known, honest miss for as long as
that was true, rather than quietly dropping the one case that exposed the gap.
It is now detected by the [IDOR probe](#broken-access-control-idor), and the
fixture declares the two identities that make it detectable at all. Reported:
**6/6 detected, 0% false positives.**

Other commands:

```bash
qagent endpoints --url http://127.0.0.1:8080     # discovered surface, ranked by risk
qagent scan --url ... --json out.json            # machine-readable results
qagent scan --url ... -H "Authorization: Bearer $TOKEN"
qagent scan --url ... --repo ./path/to/checkout  # fall back to route parsing if no OpenAPI doc
```

`qagent scan` exits non-zero when a defect is found, so it drops straight into CI.

### Test planning

Ask what QAgent *would* test, and in what order, before committing to a run:

```bash
qagent plan --url http://127.0.0.1:8080
```

```text
8 endpoints in 6 modules, 18 required checks

priority   module     endpoints  checks  why
critical   admin              1       3  Elevated-privilege surface: a defect here
                                         bypasses normal authorization.
critical   orders             2       6  Money movement: a defect risks financial
                                         loss or double charges.
high       users              1       1  Account data: a defect risks cross-tenant
                                         or cross-user data exposure.
low        products           2       4  Low risk (0.25): read-only, unauthenticated.
low        me                 1       3  Low risk (0.20): read-only, unauthenticated.
low        health             1       1  Low risk (0.10): read-only, unauthenticated.
```

Every priority names the reason that produced it, so the ranking is a claim you
can check rather than an opinion.

`--max-cases` answers the question the case limit used to hide — what a budgeted
run is about to leave untested:

```bash
qagent plan --url http://127.0.0.1:8080 --max-cases 5
# at --max-cases 5: 5/18 planned checks would run (28%)
#   left untested: users, products, me, health
```

Both critical modules survive the cut; the health check does not. That ordering
is the planner's main job — before it existed, `--max-cases` truncated in
discovery order and could spend the whole budget on `/health`.

`--checks` lists every required check, `--json` writes the plan as a document,
and `--enrich` spends one model call letting a review raise a module's priority
(never lower one, never remove a check). `qagent scan` builds the same plan
automatically and prints a coverage line whenever a run covered less than it
planned; `GET /api/v1/runs/{id}/plan` returns the stored plan for a persisted run.

### Repository RAG

A bug report that says "the handler crashed" is a claim. One that says
`app.py:98-115 (create_order)` is a lead. Point `scan` at a checkout and QAgent
indexes it, then uses each failure as a retrieval query against it:

```bash
qagent scan --url http://127.0.0.1:8080 --repo packages/fixtures/buggy-shop
```

```text
indexed 20 chunks from 1 files for root-cause evidence

GET /admin/users requires authentication        -> app.py:118-128 (admin_list_users)
POST /orders returns 201 for a valid request    -> app.py:98-115  (create_order)
POST /users returns 201 for a valid request     -> app.py:131-139 (create_user)
GET /products/{product_id} rejects a malformed  -> app.py:84-95   (get_product)
```

All four seeded defects cite the exact function that causes them. Three things
make that work, and each of them was a real failure first:

- **Symbol-level chunks, decorators included.** Python's AST puts a function's
  `lineno` at `def`, which splits `@app.get("/admin/users")` — the single most
  identifying line an endpoint has — away from its handler.
- **The culprit frame, not the whole trace.** A FastAPI 500 carries a dozen
  `starlette` and `uvicorn` frames wrapping one line of application code.
  Querying on all of them retrieves opinions about starlette. QAgent keeps only
  non-vendor frames and weights the deepest — the same heuristic error trackers
  use. Traces arrive escaped inside JSON bodies, so they are unescaped first;
  without that the frame pattern matches nothing at all.
- **Weighted route paths.** An authorization bypass leaks exactly the data some
  *other* handler returns, so the response body points confidently at the wrong
  function. The route path appears verbatim in the right handler's decorator,
  so it outweighs the body.

Search the index directly to see why a report cited what it did:

```bash
qagent search "create order price" --repo packages/fixtures/buggy-shop --code
qagent index --repo packages/fixtures/buggy-shop
```

**Retrieval is BM25 by default — no provider, no network, no API key.** That is
not a degraded mode: on code, identifier overlap is a strong signal precisely
because the thing you are searching for appears verbatim in the code that
implements it. Configure `QAGENT_EMBEDDING_PROVIDER=openai_compatible` and
embeddings are fused in by reciprocal rank fusion, which adds the paraphrase
cases lexical search cannot reach ("payment fails" → `ChargeService`). There is
deliberately no Anthropic embedder: Anthropic serves no embeddings API, and
hashed pseudo-vectors would be noise wearing the costume of a result.

**pgvector is opt-in, not automatic.** CLAUDE.md names pgvector; this project's
own compose file runs stock `postgres:16-alpine`, which does not have it.
`QAGENT_VECTOR_BACKEND` picks:

| | `json` (default) | `pgvector` |
|---|---|---|
| Requires | nothing | `pip install qagent[rag]` + the `vector` extension |
| Column | `JSON` | `vector(n)` with an IVFFlat index |
| Search | Python cosine | native `<=>` in Postgres |

Both are covered by the same integration suite, run twice in CI — once per
backend.

The first attempt at this was cleverer and wrong: store JSON, then `ALTER` the
column to `vector` at init if the extension turned out to be present. The ALTER
succeeds and then **every insert fails** with *"column is of type vector but
expression is of type json"*, because SQLAlchemy still binds the type the model
was defined with. A physical schema that disagrees with the mapper is not a
graceful degradation, it is an outage. So the column type is a declared choice
made once, and `db_init` checks both prerequisites and says so loudly when the
configuration asks for pgvector and the database cannot provide it.

Testing the native path also caught two bugs that the default path hides
completely: a type modifier bound as a query parameter (`vector(:dims)` is a
syntax error, not a slow path), and a `uuid = varchar` comparison that made
every native query fail and fall back forever — silently, because the fallback
works. That is why CI runs both.

Chunks are stored with a content digest, so re-scanning an unchanged checkout
re-embeds nothing, and a chunk whose code has moved is deleted rather than left
to cite a line number that is no longer there.

### Emitting real test files

The generator produces declarative documents, and that stays the default — a
document is diffable, reviewable, safe to execute without `eval`, and it is what
the runner, the triage stage and the eval harness all consume. `qagent emit` is
the other half of that promise: it renders the same documents as **standalone
pytest files that do not import QAgent at all.**

```bash
qagent emit --url http://127.0.0.1:8080 --out ./tests/generated
QAGENT_BASE_URL=http://127.0.0.1:8080 python -m pytest ./tests/generated
```

```text
18 test(s) across 6 file(s)

file              tests
test_admin.py         3
test_orders.py        6
test_products.py      4
...
```

One module per API module, the same grouping the planner and the dashboard use.
The suite needs only `pytest` and `httpx`, so it keeps working if QAgent is
uninstalled tomorrow — a test that only exists inside a tool is a test the
developer cannot step through or keep.

**The emitted suite reaches the same verdicts as the in-process runner** — 7
passed, 11 failed against the buggy fixture, identical to `qagent scan`. CI
asserts that equality, because "similar checks" would mean one of the two is
lying to a developer.

Nothing from a spec reaches the output as executable text. Every value goes
through `repr()` into a string literal, so a path of
`/x"); import os; os.system(...)` renders as data; the test suite asserts that
directly by parsing the output and checking no such call node exists.

### Quality gate and CI

`qagent gate` runs QA and exits non-zero when the result should stop a deploy.
It needs no database, no project and no org — a pull request has none of those,
and requiring them would mean standing up Postgres to find out whether a branch
is safe to merge.

```bash
qagent gate --url http://127.0.0.1:8080 --repo . --min-coverage 0.9
```

```text
QUALITY GATE

  critical_defects  FAIL  2 critical defect(s), limit 0
  high_defects      FAIL  5 high defect(s), limit 0
  plan_coverage     PASS  100% of the test plan ran, minimum 90%

RESULT: BLOCK
```

Exit codes: **0** deploy, **1** block, **2** nothing could be tested. That third
code matters more than it looks — a gate that reports success because it never
managed to reach the application is worse than no gate, so "I tested nothing" is
never allowed to look like "I found nothing".

Three deliberate choices:

- **It blocks on classified defects, not on red tests.** A check that failed
  because staging was down is not a reason to stop a release. Triage already
  separates the two; the gate consumes that judgement instead of re-deriving it.
- **It can block on coverage.** Once the planner can say "this run covered 40%
  of its plan", green stops meaning *nothing is wrong* and starts meaning
  *nothing that ran is wrong*. Only one of those should gate a release.
- **Every check prints its threshold and its actual value, passing or not.** A
  gate that shows only failures cannot be tuned, because nobody can see how
  close the passing checks came.

As a GitHub Action ([`action.yml`](action.yml)):

```yaml
- uses: RoshanMohamad/QAgent@master
  with:
    url: http://127.0.0.1:8080
    repo-path: .
    min-coverage: "0.9"
    # header: Authorization: Bearer ${{ secrets.QA_TOKEN }}
```

It writes a check table to the job summary and fails the job on a block; set
`fail-on-block: false` to report without failing while adopting it on an
existing codebase. Headers are passed through the environment rather than argv,
because argv is visible to every process on the runner and may carry a token.

QAgent's CI runs this action against its own buggy fixture and **asserts that it
blocks** — a gate that cannot say "no" is decoration — then runs it again with
thresholds that permit the known defects and asserts it passes.

### Browser recorder

A Chrome extension ([`apps/extension/`](apps/extension/)) records a session and
`qagent record-import` turns it into tests.

```bash
# Record → Stop → Export in the extension, then:
qagent record-import qagent-session.json --url http://127.0.0.1:8080 --out ./tests/recorded
```

```text
4 UI action(s), 5 request(s) → 3 API check(s)

method  path       body      asserts
GET     /products  none      status in [200, 201, 202, 204]
POST    /users     recorded  status in [200, 201, 202, 204]
POST    /orders    none      never a 5xx

observed  console_error: TypeError: cart is undefined
warning   1 action(s) recorded a positional selector; add a data-testid
```

The clicks become a UI flow document. **The traffic those clicks provoked
becomes API checks** — and that second half is the reason this exists. A
recorded session reaches requests static discovery cannot: they need a logged-in
user, a cart with something in it, an order that already exists.

Four things it refuses to do, each of which was a bug first:

- **Assert success for a request it cannot replay.** A `POST` whose body was not
  captured gets replayed empty, the application correctly answers 422, and a
  check asserting 2xx fails on every run while nothing is wrong. That is a false
  positive — the metric this project optimises against — so those checks assert
  only the invariant that holds regardless of input: never a 5xx.
- **Record a password.** Password-like inputs record the *action* and not the
  value, and secret-looking body fields are redacted by key name, in the
  extension and again server-side.
- **Emit a positional selector silently.** Selector priority is
  `data-testid` → stable `id` → link/button text → role → path, matching what
  [self-healing](#self-healing-selectors) scores against. A positional fallback
  is recorded as `fragile` and warned about.
- **Replay a logout.** It would invalidate the session every later check depends
  on, and the failure would read as an auth bug.

Console errors seen while recording are reported as observations: a flow that
logs an uncaught `TypeError` has already found something before a test exists.

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

**Evidence, not just a claim.** A page check that becomes a bug report captures a
screenshot and the console/page-error log at the moment of failure, stores them
(local filesystem for now — `modules/storage/local.py`; the README's own stack
table lists S3-compatible storage / Cloudflare R2 for a real deployment, and
swapping backends later touches one file), and links them to the `Bug` row as
real `Artifact` rows — the table CLAUDE.md section 15 asks for, reserved in the
schema since it was first written and unused until now. Fetch one back with
`GET /api/v1/artifacts/{id}`; `GET .../bugs` lists each bug's artifact ids and
content types. The log is scrubbed the same way any other untrusted text is
(ADR-0004) before it ever reaches storage — a live session token in a console
log is exactly the kind of thing evidence capture must never leak — and the
`Artifact.scrubbed` column records that a screenshot, unlike text, wasn't:
there's no regex over pixels, so it's stored as captured. A passing check never
captures anything; there's no reader for a screenshot of a page that worked.

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

### Deployment

`deploy/kubernetes/` carries manifests for the API, both worker pools, the
migration Job, autoscaling and a default-deny `NetworkPolicy`.

> **Validated, not proven.** Every manifest is checked by `kubeconform` in
> strict mode against the real Kubernetes schemas in CI — as raw files *and* as
> the built kustomization, since a kustomization can emit something no
> individual file contained. Strict mode is the point: it rejects unknown
> fields, the typo class that otherwise survives review and surfaces as a
> silently ignored setting in production.
>
> **Nothing here has run in a cluster under load.** Replica counts, HPA
> thresholds and resource limits are marked `# TUNE:` and carry the reasoning
> behind the starting value rather than a number dressed up as a measurement.
> Treat them as a correct starting point that still needs an SRE.

Two security properties are asserted by CI rather than trusted, because both
carry over from decisions that are already load-bearing:

- **The admin database credential reaches only the migration Job.** A superuser
  bypasses RLS unconditionally ([ADR-0007](docs/decisions/ADR-0007-rls-requires-an-unprivileged-role.md)),
  and the worker fetches user-supplied URLs. CI parses the built manifests and
  fails if `ADMIN_DATABASE_URL` appears anywhere else — a check verified by
  injecting a violation and confirming it failed.
- **The placeholder secret can never be applied.** `secrets.example.yaml`
  documents the required shape and contains `CHANGE_ME`; it is excluded from the
  kustomization, and CI greps the built output to keep it that way.

### Ephemeral environments per pull request

Reference image 02 draws a per-PR cloud environment. `ephemeral-environment.yml`
implements its shape — provision, migrate, deploy, gate, comment on the PR,
destroy — with docker compose on the runner:

```text
provision → migrate (the same db_init a rollout runs) → deploy
  → quality gate → comment → destroy   (always(), even on cancel)
```

The property that matters is that a PR is tested against a real, isolated,
freshly-migrated deployment of itself, and compose delivers that. Cloud
provisioning would add a public URL; it would also add a cloud account, a cost
owner and a teardown guarantee that survives a cancelled job. An orphaned EKS
cluster is a bill, not a bug. The provision and destroy steps are two steps —
swapping them for Terraform later changes those two and nothing else.

### On the twelve microservices

Reference image 03 draws twelve. This ships one API and two worker pools, and
[ADR-0010](docs/decisions/ADR-0010-infrastructure-is-validated-not-proven.md)
records why: splitting adds eleven deployment units, eleven failure modes, a
network hop where there is now a function call, and a tracing requirement to
debug what a stack trace answers today — without adding a capability.

What *is* built is the part carrying the actual benefit. `qagent.scan` and
`qagent.performance` are separate queues with separate Deployments and separate
autoscaling, because a load test and an API check have nothing in common
operationally. That is image 06's fan-out expressed as queue routing: a second
worker pool is a manifest, not a rewrite.

### Billing

`GET /api/v1/billing/statement` prices metered usage into line items and a
total:

```text
  QA runs                        12  USD     1.20
  AI analysis (pass-through)      1  USD     3.00
  Total                              USD     4.20
```

**It does not charge anyone.** ADR-0008 cut the seam between metering and
settlement here, and this stops at the same line: no payment provider, no card,
no tax, no currency decision. Those are a business-model decision that does not
exist yet, and an integration written against an imagined pricing page would
look finished and be discarded by the first real one. Every rate defaults to
zero, so an unconfigured deployment gets a statement with no amounts on it.

Money is `Decimal` throughout — a float subtotal is how an invoice ends up a
cent off from its own line items, and the first person to notice is a customer.

---

## Schema migrations

`db_init` brings the schema to head with Alembic. It used to call
`Base.metadata.create_all`, and replacing that was not housekeeping — it was a
correctness fix:

> `create_all` creates **missing tables** and nothing else. It will not add a
> column to a table that already exists. So the first schema change after a
> database was created worked on a fresh machine and silently did nothing
> everywhere else, surfacing much later as `column "plan" of relation
> "test_runs" does not exist` at runtime instead of at deploy time.

That is exactly how it was found here: adding the planner's `plan`/`coverage`
columns broke every already-created database while passing on a new one.

```bash
cd apps/api
ADMIN_DATABASE_URL=... alembic upgrade head     # or just run db_init
ADMIN_DATABASE_URL=... alembic revision --autogenerate -m "what changed"
```

Migrations run as the bootstrap superuser (`ADMIN_DATABASE_URL`), never as the
application role, which deliberately cannot issue DDL (ADR-0007).

Three paths are exercised against a real Postgres:

- **Fresh database** — both revisions apply, 18 tables, RLS on 16.
- **Already at head** — idempotent, no-op.
- **Pre-Alembic database with rows in it** — it has tables but no
  `alembic_version`, so `db_init` stamps the baseline and upgrades. CI asserts
  the rows survive *and* that the new `NOT NULL` columns land with a usable
  default. (`ADD COLUMN ... NOT NULL` with no default fails outright on a
  populated table — the migration adds a `server_default`, then drops it so the
  mapper stays the single source of the default.)

---

## Security scanning

Three scanners, three different questions, one vocabulary (CLAUDE.md §16).
QAgent shells out to tools maintained by people who track rule and CVE coverage
full time rather than reimplementing any of it:

| Scanner | Reads | Finds |
|---|---|---|
| **Semgrep** | source | bugs in the code this project wrote |
| **Trivy** | lockfiles | known CVEs in the code it *imported* |
| **OWASP ZAP** | the running app | missing headers, insecure cookies, reflected input |

```bash
qagent security --repo .                 # semgrep + trivy
qagent dast --url http://127.0.0.1:8080  # zap, against a running app
```

They disagree about everything — Semgrep says `ERROR`, Trivy says `CRITICAL`,
ZAP says `riskcode: 3` — so every finding is normalised into one severity scale
and deduplicated across tools, with the *higher* severity winning a
disagreement, because under-reporting a real vulnerability is the more expensive
mistake.

Two choices worth stating:

- **A missing scanner is a recorded skip, not a failure.** They have three
  different installation stories and almost nobody has all three on day one; a
  scan that refuses to start is a scan that gets deleted from CI. But the skip
  is never silent, and when nothing ran at all the report says so outright —
  *"no security scanner could run; this result says nothing about the project"*.
  "No findings" and "nothing ran" must never look the same.
- **ZAP is passive by default.** `--active` sends injection and traversal
  payloads and can mutate state, so it is opt-in and announced. A QA tool that
  attacks a host because a flag defaulted to true is a liability.

ZAP alerts the scanner *itself* marks as false positives are dropped rather than
reported: false-positive rate is this project's primary metric (ADR-0003), and
rows the tool disbelieves must not attack it.

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

### Defect history and alerts

A `Bug` row carries the current status and nothing else, which cannot answer the
questions a defect is actually asked: how long has this been open, and did it
come back after we closed it. `bug_events` answers both.

It also fixed a real miscount. Persistence used to insert a defect row
unconditionally, so one bug surviving ten runs became ten `BUG-` references and
the dashboard counted it ten times. Identity is now the *test case* — not the
title, which a model rewrites between runs — so a repeat is an update plus a
history entry:

```text
GET /bugs/{id}/history

opened          → high
reproduced
reopened        closed → open     ← a regression, not a new defect
severity_changed  high → critical
reopen_count: 1
```

The retrieved "affected code" from [repository RAG](#repository-rag) is written
alongside as a **generated comment** (`generated: true`), so a machine's opinion
sits next to the human discussion and is never mistaken for it.

**Alerts fire on their own.** A blocked gate and a critical defect send to a
Slack or webhook target configured per *project* — per deployment would let one
org's defects page another org's channel. The default event set is short on
purpose: a notifier that fires on everything gets muted, and a muted notifier is
worse than none because the team stops watching the dashboard expecting to be
paged.

Delivery is recorded rather than assumed. A failed send leaves a row with the
error and an attempt count, and `qagent.retry_notifications` turns that row back
into an attempt — up to five, after which it is marked `abandoned` rather than
sitting at `failed` forever, indistinguishable from one still waiting.

```bash
PUT /api/v1/projects/{id}/notify-config   # where alerts go
GET /api/v1/projects/{id}/notifications   # what was delivered, and what failed
```

Gate decisions are recorded the same way. `qagent gate --report-to $QAGENT_API_URL
--project $ID` stores the verdict *with the policy and per-check numbers it was
made from*, because recomputing it later against today's open defects gives a
different and useless answer. Reporting can never fail the gate: a QAgent API
having a bad minute must not turn a passing build red.

### Broken access control (IDOR)

The one class of defect a generator rule cannot reach. Every rule builds one
self-contained request from an endpoint's shape; IDOR is by definition the
*difference between what two identities can reach*, and confirming it needs
state — you must learn an identifier belonging to user A before asking whether
user B can read it.

So it is a probe, not a rule:

```bash
qagent idor --url http://127.0.0.1:8081   -H "Authorization: Bearer alice-token"   --header2 "Authorization: Bearer bob-token"
```

```text
1 endpoint(s) probed with two identities
1 finding(s)

Broken object-level authorization (IDOR)
endpoint   GET /tasks/{task_id}
severity   high        cwe  CWE-639

A second authenticated identity received the identical response for /tasks/1 as
the identity that owns it (HTTP 200).
```

Three steps: list a collection as A, take an identifier, request it as B. Four
things it refuses to do, because an access-control scanner that cries wolf gets
switched off:

- **A 200 is not evidence.** Plenty of APIs legitimately return a shared or
  filtered resource to anyone authenticated. Only an *equal* response —
  compared as parsed JSON, so formatting can neither hide a leak nor fake one —
  proves B read A's row.
- **Write methods are never probed.** Confirming that B can `DELETE` A's order
  requires deleting A's order.
- **An unprotected endpoint is not an authorization failure**, it is a public
  endpoint.
- **"Could not check" is never reported as "clean."** An endpoint with no
  listable collection comes back `inconclusive`, with the reason.

Supply a second identity to `run_pipeline` and the probe joins the normal loop,
folding findings in as `api_security` so they share one persistence path, one
dashboard and one quality gate with everything else.

**It runs before the generated cases, not after** — which was a bug first. The
probe is read-only and needs the application's data as it found it; generation
deliberately produces destructive cases, and `DELETE /tasks/1` had already
removed the row the probe was about to use as evidence. A read-only check
downstream of a destructive one is measuring a different application.

### Evidence on every defect

A defect report that says "the handler returned 500" is a claim the reader has
to take on faith and retype by hand to reproduce. Every defect now ships with a
**HAR** — the format Chrome DevTools, Insomnia, Postman, Charles and Fiddler all
import — so reproducing it is dropping a file into a tool the developer already
has open, with no QAgent involved.

| Defect from | Evidence |
|---|---|
| API check | HAR (replayable request + response) |
| Browser page check | Screenshot, console log |
| Interactive exploration | Screenshot, console log |

The last row is new, and its absence was not a decision — nothing had wired it
up, which left the hardest-to-reproduce findings the least documented. Both
browser stages now share one helper so they cannot drift apart again.

**A HAR records headers verbatim; that is the point of the format and also the
risk.** An unscrubbed one is a live bearer token in a file built to be shared.
So credential-bearing headers are replaced outright rather than pattern-matched
(a session cookie has no shape to recognise), the body scrubber runs over
everything else, bodies are truncated, and `persistence.py` scrubs again on the
way to storage — which is what lets the `scrubbed` column be true rather than
aspirational.

Evidence is attached only to outcomes that became defects. A screenshot of every
passing page load is storage cost with no reader.

### Quality score

CLAUDE.md section 4 puts `87/100` at the top of the dashboard. A single number
summarising a codebase is the easiest thing in this project to do badly — done
badly it moves for reasons nobody can trace, and becomes decoration.

```text
QA HEALTH

Overall Score                 78/100  (watch)

  defects       -10.0 pts     3 open (1 at high or critical)
  security       -2.0 pts     2 open finding(s)
  coverage       -4.0 pts     80% of the discovered API surface exercised
  reliability    -1.0 pts     2 flaky, 0 errored of 40
  performance   not measured  no load test has run

Fix first: defects
```

Three rules keep it honest:

- **Every point lost is attributable.** The score is 100 minus *named*
  penalties, returned with the breakdown and a `fix_first`. "87/100" alone is
  not a product; the breakdown is.
- **Nothing unmeasured is scored.** A project with no load test is not penalised
  for it — the dimension is marked `not_measured` and the remaining weights
  renormalise. Otherwise the score rewards running scanners rather than fixing
  defects, and a team that cannot run one watches it sit low forever.
- **An unmeasured project is `not measured`, not `critical`.** It scores 0 —
  a new project must not look perfect — but grading that "critical" is a
  different lie, and one that teaches the user to distrust the number before it
  has told them anything.

Weights are stated in `modules/quality/score.py` rather than hidden. They are a
judgement call, and writing them where they can be argued with is the honest
form of that. Open defects dominate deliberately: a score where "we ran a load
test" offsets "two critical bugs are open" is measuring the wrong thing.

### Coverage, storage, tracing

Three smaller pieces, each with one decision worth stating.

**Coverage is *surface* coverage, and says so.** CLAUDE.md's dashboard mock asks
for "Backend 82%", which reads as line coverage — a number this tool cannot
honestly produce, because it tests the application as a black box and never
instruments it. So what is reported is the share of the discovered API surface
that a check actually ran against, and the payload carries the sentence *"Not
line coverage"* so the figure cannot be misread:

```text
surface 2/8 discovered endpoints exercised (25%)
  untouched: POST /orders, POST /users, GET /products/{product_id}, GET /me
```

The percentage is the headline; the `untouched` list is the actual next action.
Coverage is credited only for endpoints a check *executed* against — never for
ones merely discovered or merely planned — so a run truncated by `--max-cases`
shows as missing surface rather than as success.

**Artifact storage is pluggable.** `QAGENT_STORAGE_BACKEND=s3` switches evidence
to any S3-compatible service (S3, R2, MinIO); the default is a directory on
disk. Both backends share one key layout (`<org>/<kind>/<sha256><ext>`), so
migrating a filesystem root into a bucket is a recursive copy rather than a
script. Content-addressed, so the same screenshot captured by two checks is
stored once. Configuring `s3` without a bucket is refused rather than silently
demoted to local storage — a deployment that believes its evidence is durable,
and is really writing to a container filesystem, loses exactly what a bug report
depends on.

**Tracing answers the question the counters cannot.** Prometheus says a scan
took 40 seconds; it cannot say whether that was discovery waiting on a slow
OpenAPI fetch or triage waiting on a model, and those have opposite fixes. A
span per stage says it directly:

```text
qagent.discover   423.7ms  {endpoints: 8, spec_url: .../openapi.json}
qagent.plan         0.6ms  {modules: 6, required_checks: 18}
qagent.generate     0.3ms  {cases: 18, coverage_ratio: 1.0}
qagent.execute    176.5ms  {cases: 18, passed: 7, failed: 11}
```

Off by default and genuinely free when off: `span()` is a null context manager
and nothing imports `opentelemetry`. This partially supersedes
[ADR-0008](docs/decisions/ADR-0008-phase-6-scope.md), which deferred tracing —
that reasoning was right about *distributed* tracing and sampling policy, and
wrong about instrumentation. Sampling, retention and backend choice are still
deferred; OTLP keeps them the operator's.

### Performance testing

Two generators, one metric shape:

```bash
qagent perf --url http://127.0.0.1:8080 --vus 100,500,1000
qagent perf --url http://127.0.0.1:8080 --tool jmeter
```

k6 stays the default — one Go binary, a JSON summary, no XML. JMeter is
supported because CLAUDE.md names it and because a team with existing JMeter
expertise, test plans and CI should not have to abandon them to use QAgent.

They report differently and the difference is not cosmetic: k6 hands back
pre-aggregated metrics, JMeter writes a per-sample CSV and expects the reader to
aggregate. So QAgent computes JMeter's percentiles itself, using nearest-rank to
match JMeter's own HTML report — two tools disagreeing about what "p95" means
while writing into the same column would make the number incomparable across
runs with nothing looking wrong.

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

### Repository analysis

Agent 1, the Project Analyst (CLAUDE.md §6-8): detect what a checkout is built
out of and build its module tree, from a repo alone — nothing here needs the
app running:

```bash
qagent analyze --repo ./path/to/checkout
```

Or connect a GitHub repository directly and let QAgent fetch it:

```bash
qagent connect --repo https://github.com/pallets/flask      # or just pallets/flask
qagent connect --repo me/private-app --token $GITHUB_TOKEN  # private
```

```text
+------------------- QAgent connect -------------------+
| https://github.com/pallets/flask @ d73fa1cd          |
| 6 technologies - 137 endpoints - 0 frontend routes   |
+------------------------------------------------------+
  Flask 2.3.2 - Redis 4.5.4 - Celery 5.2.7 - pytest
```

The same thing over the API, which also persists the result to the project:

```bash
curl -X POST $API/api/v1/projects/$PID/connect -H "Authorization: Bearer $TOKEN" \
  -d '{"repo_url": "https://github.com/me/app", "branch": "main"}'
# -> {"repo_url": ..., "commit_sha": "d73fa1cd...", "summary": {...}}
```

The clone is shallow, single-branch, submodule-free, pinned to `github.com`,
and **deleted as soon as the analysis finishes** — nothing downstream reads
source after Agent 1, and checks run against a running application, not files.
Only https `github.com` URLs are accepted: `git clone` otherwise honours
transports that execute commands (`ext::`), read local files (`file://`), or
reach internal hosts the API process can see and the caller should not, and one
host allowlist removes all three at once. A token is used for the clone and
nothing else — it is passed through a 0600 credential file rather than argv (so
it is not in `ps`), stripped from git's own error output, and never written to
the database (ADR-0009).

Analysis is a snapshot, not a subscription: a later push does not update it.
Webhook-driven re-analysis needs a public callback and is deferred with Phase 5.

Detection reads manifests and config files only (`package.json`,
`pyproject.toml`/`requirements.txt`, `docker-compose.yml`, framework config
files, `.env.example` templates) and parses them with stdlib/YAML parsers —
never `npm install`, never importing the checkout, never a live `.env` (only
`.env.example`-style templates, and only variable *names*, never values).
Every detection carries its evidence — the file and the literal marker that
proved it — and a technology confirmed from two independent sources (a
dependency *and* its config file) is corroborated rather than listed twice
(ADR-0006).

The module tree groups whatever discovery already found — an OpenAPI document,
or the static route parser's fallback — by leading path segment (`/api/v1/orders/{id}`
→ `orders`), reads frontend routes off Next.js's own App/Pages Router directory
convention, and lists database models from ORM source (`__tablename__`, Prisma
`model`, Django `models.Model`). A module is flagged risky by name (auth,
payment, admin, checkout, token, ...) or because one of its endpoints scores
≥0.6 on the same risk heuristic that already orders test generation — one
number, not two disagreeing ones, for "how much attention does this deserve."

Pass `--url`/`--spec` too and endpoint grouping uses the real OpenAPI surface
instead of falling back to route parsing, same contract as `qagent endpoints`.
`POST /api/v1/projects/{id}/analyze` runs the identical pass against a
`repo_path` the API process can read and persists the result into
`Project.stack` — the column CLAUDE.md's own schema reserved for this and,
until now, nothing wrote.

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
cp .env.admin.example .env.admin   # bootstrap-only superuser DSN - api only, never worker (ADR-0007)
docker compose up --build
```

### Access control, rate limits, usage and metrics

`register` always mints exactly one user, "owner" — an owner adds anyone else:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/users -H "Authorization: Bearer $TOKEN" \
  -d '{"email": "teammate@example.com", "password": "...", "role": "member"}'
```

A member can do everything except read an arbitrary local path (`analyze`,
`security/scan`), send real sustained traffic (`performance/scan`), provision an
environment, or manage membership — those need an owner (ADR-0008), and RLS
already confines *both* roles to their own organization's data regardless.

`register`/`login` are rate-limited by IP, `runs`/`performance/scan` by
organization (Redis-backed, fails open on a Redis outage — same rule as the LLM
budgets). A billing period's usage:

```bash
curl http://127.0.0.1:8000/api/v1/usage -H "Authorization: Bearer $TOKEN"
# {"runs": {"passed": 12, "failed": 3}, "llm": {"calls": 40, "spend_usd": 0.81}, "defects": {"high": 2}}
```

And `GET /metrics` (Prometheus text format, no auth — matching a scraper's own
contract): HTTP requests by route template and status, finished runs by status,
defects by severity, cumulative LLM spend.

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

### 2. Plan

The **Test Planner** (agent 2) groups the discovered endpoints into modules —
the same grouping the analyst report and the dashboard show — ranks each module
`critical`/`high`/`medium`/`low`, and states the checks that module requires.
Every priority carries the reason that produced it: a matched risk keyword
(`auth`, `payment`, `admin`) or the numeric risk score.

The plan then does two things generation could not do on its own:

- **It orders the case budget.** `--max-cases` used to truncate in discovery
  order, so a capped run could spend everything on a health check and never
  reach the auth module. It now truncates lowest-priority-first.
- **It reports what it left out.** `coverage` matches the plan's
  `(endpoint, rule)` pairs against what generation actually emitted, so a run
  that covered 60% of its plan says so and names the untested modules. A tool
  that silently covers less than it claimed is worse than one that covers less
  and says so.

The plan is rules-derived and free. `--enrich` spends one model call letting a
review *raise* a module's priority; it can never lower one or remove a check,
so a prompt-injected repository cannot talk the planner out of testing auth.

### 3. Generate

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

### 4. Execute

An SSRF-guarded HTTP runner. Assertions are data, never expressions, so a generated
or model-suggested test can never execute arbitrary logic inside the platform.

### 5. Triage & report

Rules classify, the model arbitrates ambiguity, and only real defects become bug
reports with reproduction steps, expected vs. actual, root cause and a suggested fix.

When a checkout is available, the failure is also used as a retrieval query
against the indexed source, and the matching functions are attached as
`affected_code` — the "Affected: `OrderService.createOrder()`" line CLAUDE.md
section 13 asks for. The location the model may cite is a **closed enum built
from what was actually retrieved**, plus `unknown`: it cannot name a file it was
not shown, because the schema has no value for one. That turns "please do not
hallucinate a filename" from an instruction into an impossibility.

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
| [0006](docs/decisions/ADR-0006-repository-analyzer.md) | The repository analyzer reads and parses text only — never executes a checkout's own tooling — and every detection carries its evidence. |
| [0007](docs/decisions/ADR-0007-rls-requires-an-unprivileged-role.md) | The API/worker connect as a separate, unprivileged role — never the superuser that bootstraps the schema — or row-level security is silently bypassed. |
| [0008](docs/decisions/ADR-0008-phase-6-scope.md) | Phase 6 built RBAC, rate limiting, metrics, usage tracking and queue separation now; payment integration, cluster autoscaling and distributed tracing stay deferred until there's a real deployment to size them against. |
| [0010](docs/decisions/ADR-0010-infrastructure-is-validated-not-proven.md) | Infrastructure is schema-validated in CI, never described as proven; billing prices but never settles; the twelve-microservice split stays unbuilt and the queue split is the fan-out that carries its benefit. |
| [0009](docs/decisions/ADR-0009-github-checkouts-are-ephemeral-and-host-pinned.md) | A connected repository is cloned from `github.com` only, with command-executing and local-file git transports disabled, and the checkout is deleted once analysed — tokens never reach argv or the database. |

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
  call site — enforced only because the API/worker connect as a separate,
  unprivileged role and never as the superuser that bootstraps the schema
  (ADR-0007): a superuser bypasses RLS unconditionally, `FORCE` included.
- **Budgets.** Per-run caps on calls, tokens and spend. Exceeding one degrades to the
  rule-based path rather than failing the run.
- **RBAC.** `require_owner` (ADR-0008) gates reading an arbitrary local `repo_path`
  (`analyze`, `security/scan`), sending real sustained traffic (`performance/scan`),
  provisioning an environment, and managing org membership. Every other authenticated
  action stays reachable by any member — RLS already stops cross-*tenant* access
  regardless of role, so this is about actions dangerous within one's own org.
- **Rate limiting.** Redis-backed, fixed-window: IP-keyed on `register`/`login`
  (a password-guessing oracle otherwise), org-keyed on `runs`/`performance/scan`
  (nothing else stops one tenant saturating the shared Celery queue). Fails open,
  not closed, on a Redis outage — same rule as budgets above.

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
├── cli.py                 qagent scan | analyze | endpoints | evaluate
├── modules/
│   ├── analyzer/          Project Analyst: stack detection, module tree (agent 1)
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
│   ├── storage/           evidence artifact backend (local now, S3-compatible later)
│   └── llm/               providers, budgets, safety boundary
├── eval/harness.py        scores the pipeline against ground truth
├── db_init.py             schema, the unprivileged app role, RLS (ADR-0007)
└── worker/tasks.py        Celery
apps/api/tests_integration/  real Postgres + Redis: RLS isolation, a live worker,
                              RBAC, rate limiting, usage (ADR-0007, ADR-0008)
apps/web/                  Next.js dashboard (server components, no client fetching)
apps/web/tests/            Vitest + Testing Library: components, lib/api.ts, middleware
packages/fixtures/         apps with labelled, seeded defects (buggy-shop: FastAPI,
                           task-tracker: Flask, incl. one documented detection gap)
docs/decisions/            ADRs
```

## Tests

```bash
cd apps/api && pytest tests -q     # 318 tests, no database needed
```

Against a real Postgres + Redis (`db-integration` in CI, ADR-0007):

```bash
POSTGRES_PORT=55432 REDIS_PORT=56379 docker compose up -d postgres redis
export ADMIN_DATABASE_URL=postgresql+psycopg://qagent:qagent@localhost:55432/qagent
export DATABASE_URL=postgresql+psycopg://qagent_app:qagent_app@localhost:55432/qagent
export REDIS_URL=redis://localhost:56379/0
export CELERY_BROKER_URL=redis://localhost:56379/1
export CELERY_RESULT_BACKEND=redis://localhost:56379/2
cd apps/api && pytest tests_integration -q
```

The dashboard has its own suite — component/unit tests (Vitest + React Testing
Library), independent of the Python one above:

```bash
cd apps/web && npm test        # 51 tests, jsdom, no backend needed
```

Every client component, `lib/api.ts`'s request/error handling (401 with no
token, a network failure mapped to the same 503 `SetupNotice` renders on, a
non-ok response, success), and `middleware.ts`'s redirect are covered — 100%
statement coverage on `components/`. Server components that fetch data
(`app/page.tsx` and friends) are exercised instead by `npm run build`, which
type-checks every page against the real API response shapes in `lib/types.ts`,
and by the pipeline's own eval-fixture runs (README "Status") which is what
actually produces the data those pages render in practice — there's no headless
browser here re-clicking through the app; that's what `qagent explore --check`
(above) is for, pointed at a running dashboard.

CI runs lint, unit tests (API and dashboard), **and the evaluation harness** —
a change that degrades detection or raises false positives fails the build.

---

## Roadmap

**CLAUDE.md phases 1-5 are complete.** The last items closed were the Playwright
emitter (`qagent record-import --playwright`, verified driving a real browser),
the IDOR probe ([`qagent idor`](#broken-access-control-idor)) which took
[task-tracker](packages/fixtures/task-tracker) from 5/6 to 6/6,
[HAR evidence](#evidence-on-every-defect) on every defect, the
[quality score](#quality-score), and JMeter alongside k6.

What remains is deliberately deferred rather than pending — see below, and
[ADR-0008](docs/decisions/ADR-0008-phase-6-scope.md). The honest list of things
that would come next, none of which are CLAUDE.md requirements:

- **Playwright `trace.zip`** on browser defects. The HAR covers the network
  side; a trace adds DOM snapshots and is strictly better evidence, at the cost
  of tens of megabytes per failure — worth doing once there is a retention
  policy to size it against.
- **GitLab and Bitbucket** (§5 says GitHub first, and only GitHub is built).
- **Settlement.** The statement is priced; wiring a payment provider needs a
  pricing decision that does not exist yet ([ADR-0010](docs/decisions/ADR-0010-infrastructure-is-validated-not-proven.md)).
- **Tuning the manifests against a real cluster.** They are schema-valid; the
  numbers marked `# TUNE:` are starting points, not measurements.
- **More fixtures**, which is the highest-leverage work at any point — every
  metric in this README is only as trustworthy as the ground truth behind it.

Phase 6 (CLAUDE.md §23) is scoped, not skipped — see
[ADR-0008](docs/decisions/ADR-0008-phase-6-scope.md). Built: RBAC (`require_owner`
gates the actions that read an arbitrary local path, send real traffic, or manage
org membership; `POST /api/v1/users` closes the gap where there was previously
no way to have a second user in an organization at all), rate limiting
(Redis-backed, IP-keyed on auth endpoints, org-keyed on run/performance-scan
triggers, fails open on a Redis outage), observability (`GET /metrics`,
Prometheus format), usage tracking (`GET /api/v1/usage`, windowed — not
`/dashboard`'s all-time spend), and job-queue separation (`qagent.scan` /
`qagent.performance`, so load-test capacity scales independently of ordinary
scans). Deliberately not built: payment/invoicing integration, Kubernetes
manifests or autoscaling policies, and distributed tracing — all three need a
real deployment or real load to size against, and ADR-0001 is exactly the
argument against building them on guesses.

Everything still open across phases 1-5 is listed once, above — this paragraph
used to repeat that list and the two copies had already started to disagree.

More fixtures are the highest-leverage work at any point: every metric above is only
as trustworthy as the ground truth behind it.
