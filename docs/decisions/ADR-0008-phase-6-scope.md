# ADR-0008: Phase 6, scoped to what's buildable and testable now

**Status:** Accepted
**Date:** 2026-09-18

## Context

CLAUDE.md section 23 (Phase 6 — Production Architecture) lists eight items: multi-tenancy,
RBAC, distributed workers, job queues, observability, rate limiting, billing/usage,
scalable infrastructure. ADR-0001 already establishes the project's governing rule for
work like this: don't build ahead of load that doesn't exist yet. Phase 6 is exactly
where that rule is most tempting to break, because every item on the list sounds like
infrastructure a "real" platform should already have.

Three of the eight are not application code at all - they're operational commitments
that only mean something against a real deployment:

- **Billing** (the part CLAUDE.md's "billing/usage" pairs together) means an invoice, a
  payment provider integration, and a dunning process. Building that against zero paying
  organizations produces code nobody has validated against a real payment flow, which is
  worse than not having it: it looks done and isn't.
- **Scalable infrastructure** means autoscaling policies, a cluster, and a load profile
  to scale against. A Kubernetes manifest nobody has run, sized against no observed
  traffic, is guesswork committed to the repository as if it were fact.
- **Full distributed scheduling** (reference image 05's Argo Workflows topology, cited
  in worker/tasks.py's own docstring) is the right answer once the platform is on
  Kubernetes and needs it - not before.

The other five are ordinary application code with a clear, checkable "does this work"
answer, and the RBAC gap in particular was already a live hole, not a future one: any
authenticated member could read an arbitrary local filesystem path (`analyze`,
`security/scan`), send real sustained traffic to any target (`performance/scan`), or
provision an environment's credentials. Nothing distinguished a member from the owner
who created the organization.

## Decision

Built, in this pass:

- **RBAC.** `has_role`/`ROLE_RANK` (modules/auth/security.py) rank "member" below
  "owner"; `require_owner` (main.py) gates `analyze`, `security/scan`,
  `performance/scan`, environment creation, and inviting a new user. Everything else
  stays reachable by any authenticated member - RLS already stops a member from
  touching another *organization's* data regardless of role, so this is specifically
  about actions dangerous within one's own org.
- **Multi-tenancy, the missing half.** There was no way to add a second user to an
  organization at all before this - `register` always minted exactly one, "owner".
  `POST /api/v1/users` (owner-only) and `GET /api/v1/users` close that; RBAC without a
  way to have more than one role represented in an org was untestable in practice.
- **Rate limiting.** `modules/ratelimit/limiter.py`: a fixed-window counter, keyed by IP
  for the unauthenticated auth endpoints and by organization for the two
  queue-triggering endpoints (`runs`, `performance/scan`). Deliberately the simplest
  thing that stops a runaway script - not a sliding window, not billing-grade metering.
  Fails open on a Redis outage, matching the LLM budget's own "degrade, don't take the
  feature down" rule (README Security section).
- **Observability.** `modules/observability/metrics.py`: Prometheus counters/histogram
  for HTTP requests (by route template, never a raw path - an unbounded-cardinality
  label is how you take down your own Prometheus server), finished runs by status,
  defects by severity, and cumulative LLM spend. Scraped at `GET /metrics`. This answers
  "is it up and is it finding anything," not "trace this one request across services" -
  the latter needs the distributed system Phase 6 doesn't build yet.
- **Usage.** `GET /api/v1/usage`: the same three numbers a billing period would meter
  against (runs, LLM spend, defects), windowed rather than all-time. This computes what
  a billing system would need; it does not send an invoice, because there is no payment
  provider to send one through.
- **Job queue separation.** `worker/tasks.py`'s `task_routes` splits scans onto
  `qagent.scan` and load tests onto `qagent.performance`. One worker consuming both
  (docker-compose.yml's default) behaves exactly as before; an operator who actually
  needs to scale load-test capacity independently of ordinary scans now can, by pointing
  a second worker at only `qagent.performance`, with no code change.

Deliberately not built:

- Payment/invoicing integration, or any payment provider's SDK.
- Kubernetes manifests, autoscaling policies, or any infrastructure-as-code - there is
  no observed load to size them against, and untested infra code is a liability, not an
  asset.
- Distributed tracing / APM. The two-queue split above is real horizontal scaling for
  the one place this system currently needs it; a tracing system answers a question
  ("why is this one request slow across N services") that doesn't arise until there are
  actually N services.

## Consequences

- An operator who wants real billing wires a payment provider against `GET
  /api/v1/usage`'s numbers - the metering is done, the settlement isn't, and that seam is
  exactly where it should be cut.
- `require_owner` is deliberately coarse - two roles, one gate function. A third role
  (e.g. a read-only "viewer") is one entry in `ROLE_RANK` and a second gate, not a
  redesign, per `has_role`'s own docstring.
- The rate limiter's fixed-window trade-off (a caller can burst to roughly `2 * limit`
  across a window boundary) is accepted for the same reason ADR-0003 accepts a
  rules-first classifier over a fancier one: simple, free, and enough for the actual
  threat model, with the fancier version staying available if traffic ever demands it.
