# ADR-0002: API testing lands before browser E2E

**Status:** Accepted
**Date:** 2026-09-08

## Context

`CLAUDE.md` Phase 1 ends at Playwright execution. Generated browser tests against an
unfamiliar application fail constantly, and a failure cannot be attributed: "the generated
selector is wrong" and "the application is broken" look identical. The product's entire
value proposition (failure classification, bug reports, suggested fixes) dissolves in
that ambiguity, and the first demo produces noise.

## Decision

The first executable surface is the **API test engine**, not Playwright.

Endpoints are discovered from an OpenAPI document or by parsing routes. Generated cases
are HTTP requests with assertions. Execution is deterministic: no selectors, no browser,
no flake. `500` where `400` was specified is unambiguously a defect.

Playwright, the Explorer Agent and the state graph come after the
analyze -> generate -> run -> triage loop is closed and measured.

## Consequences

- Earlier trustworthy signal; failure triage has a clean ground truth to learn against.
- Selector self-healing (`CLAUDE.md` §11) is deferred with the browser work.
