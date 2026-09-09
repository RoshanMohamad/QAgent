# ADR-0003: The evaluation harness is a first-class component

**Status:** Accepted
**Date:** 2026-09-08

## Context

`CLAUDE.md` contains no way to measure whether the AI layer works. Without one, prompts
cannot be tuned, models cannot be compared, and no quality claim is defensible.

## Decision

`packages/fixtures/` holds applications with **deliberately seeded, labelled defects**.
`qagent eval` runs the full pipeline against them and reports:

| Metric | Definition |
|---|---|
| `detection_rate` | seeded defects found / seeded defects present |
| `false_positive_rate` | reported defects with no seeded counterpart / total reported |
| `triage_accuracy` | failure classifications matching the label |
| `cost_per_project` | USD and tokens consumed per full analysis |

`false_positive_rate` is the primary metric. A QA tool that cries wolf is discarded by
its users regardless of recall.

## Consequences

- Every prompt or model change is measurable against a fixed baseline.
- Fixtures must ship their own ground-truth manifest (`seeded_defects.yaml`).
