# ADR-0001: Scope and the input contract

**Status:** Accepted
**Date:** 2026-09-08

## Context

`CLAUDE.md` describes a six-phase platform. The hardest unsolved problem in it is not
AI quality — it is that no component brings the user's application *up* so that there is
something to test. Running an arbitrary repository requires dependency install, a database,
migrations, seed data, secrets, service ordering, a health check and a base URL. That is not
solvable in general.

## Decision

QAgent accepts a project only through one of two input contracts:

1. **`target_url` mode (default, MVP).** The user points QAgent at an already-running
   application and optionally supplies an OpenAPI document. QAgent provisions nothing.
2. **`compose` mode (Phase 2).** The repository contains a `docker-compose.yml`. QAgent
   brings the stack up, waits on healthchecks, runs, tears down.

Anything else is out of scope. Ephemeral Kubernetes environments (reference image 02)
are explicitly deferred.

## Consequences

- The MVP works on day one with zero provisioning machinery.
- Test execution is decoupled from environment provisioning, so `compose` mode is
  additive rather than a rewrite.
- Repositories without a running instance or a compose file cannot be onboarded. Accepted.
