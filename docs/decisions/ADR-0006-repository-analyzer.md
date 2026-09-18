# ADR-0006: Repository analysis is declared-evidence-only, never executed

**Status:** Accepted
**Date:** 2026-09-18

## Context

CLAUDE.md section 6 (Repository Analyzer) and section 8 Agent 1 (Project Analyst) ask
for a component that looks at a connected repository and answers "what is this thing
built out of, and what does it expose" — frontend/backend framework, database, Docker,
auth, test frameworks, plus a module tree (section 7) grouping the API surface the way
the CLAUDE.md example groups it: Auth API, Product API, Order API.

The obvious way to answer "what framework does this use" is to run the project's own
tooling — `npm ls`, import the app module and inspect its router, run the test suite and
see what fails. All three are refused by decisions already on the books:

- ADR-0001 accepts a project only as a running URL or a compose file; nothing here
  provisions or executes an arbitrary checkout to inspect it.
- ADR-0004 treats repository content as untrusted input. Executing a checkout's own
  build/test tooling to learn what it is would run attacker-controlled code before a
  single test has been generated, which is exactly the boundary ADR-0004 draws.
- Section 22 requires sandboxing before third-party code runs at all. The analyzer is
  meant to be the *first* thing that touches a freshly connected repository, often before
  any sandbox has been provisioned — it cannot assume one exists yet.

## Decision

The analyzer only reads text and only parses it with well-known, non-executing parsers
(`json`, `tomllib`, `ast`, `yaml.safe_load`, regex over source). It never runs `npm
install`, never imports application code, never shells out to the project's own build or
test tooling, and never opens a live `.env` — only `.env.example`-style templates, and
only variable *names*, never values.

Every detection carries its evidence: the file and the literal marker (a package name, a
config filename, a compose image) that produced it, ranked by how strong that evidence is
— a declared dependency outranks a config file's mere presence, which outranks a
same-named container image next to other services. The same technology proven from two
directions is corroboration, not two findings, and its confidence rises accordingly. This
mirrors ADR-0003's own insistence on falsifiable, inspectable output over a confident
label nobody can check.

The module tree (section 7) is built entirely from what discovery already computed —
endpoints from an OpenAPI document or the existing static route parser (`discovery/routes.py`)
— grouped by leading path segment, plus frontend routes read off Next.js's own
App/Pages Router directory convention (a directory convention *is* the routing table for
that framework, unlike Express/Flask/FastAPI, where a route only exists once a decorator
executes). Database models are read from ORM declarations (`__tablename__`, Prisma
`model`, Django `models.Model`) the same way — text, never a connection.

A module is flagged "risky" by two independent signals: its name matching a keyword
CLAUDE.md's own examples call out by name (auth, payment, admin, checkout, user, token,
webhook, upload), or an endpoint inside it scoring ≥0.6 on the risk heuristic
`discovery/openapi.py` already uses for generation priority. No score is invented here —
the same number that decides generation order also decides analyst attention, so a
reviewer never has to reconcile two different "how risky is this" numbers for the same
endpoint.

This is Agent 1, not a model call. Every field is deterministic and reproducible run to
run, for the same reason the triage classifier and generator rules are rules-first
(README "Why triage is the product"): free, instant, and testable without a live
provider. A model has nothing to add to "does this repo's `package.json` list `react`."

## Consequences

- Detection is conservative by construction: an unlisted dependency, a framework used
  through an unrecognised meta-framework, or a route defined through a factory or a
  config-loaded table is invisible, the same honest gap ADR's for the static route
  parser already accept rather than paper over with a guess.
- The analyzer runs before, and independently of, whatever sandbox eventually executes
  the repository's own code — it can run today, safely, on a repository nobody has
  provisioned a sandbox for yet.
- `Project.stack` (reserved in `models.py` since the schema was first written) now has a
  real writer: `qagent analyze` and `POST /api/v1/projects/{id}/analyze` both produce and
  persist the same `ProjectAnalysis.to_dict()` shape, so the CLI, the API, and (eventually)
  the Test Planner agent all consume one representation.
- Extending detection to a new framework or ORM is adding one table entry or one regex,
  not a new subsystem — the same shape of extension ADR-0001 designed the route parser
  and generator rules to have.
