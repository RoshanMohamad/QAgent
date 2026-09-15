# ADR-0005: Interactive exploration — state identity, closed actions, rules-first ranking

**Status:** Accepted
**Date:** 2026-09-15

## Context

The Explorer Agent (`modules/explorer/crawler.py`) only ever followed `<a href>` links.
CLAUDE.md sections 8-9 describe a fuller loop — open the app, identify interactive
elements, choose an action, observe the result, update a state graph, choose the next
action — and the README named this the one open item left on the original roadmap:
"choosing which button to click meaningfully ... needs either heuristics tied to element
semantics or a model in the loop, and either way needs the state graph's dead ends and
duplicate states worked out first."

Three problems had to be solved before any of that was safe to build:

1. **State identity.** An action can change the page without changing the URL (a form
   submits into a "thanks" state, a modal opens). Bare-URL node identity — what
   `crawler.StateGraph` uses — can't tell that state apart from the page before the
   action, so it either stops exploring the moment anything changes, or (if identity is
   dropped entirely) loops forever.
2. **Untrusted actions.** The DOM is attacker-controlled content (ADR-0004). Whatever
   decides "click this" must never let page content, or a model reasoning over page
   content, expand the set of actions available beyond what code already extracted.
3. **Destructive defaults.** An agent that fills forms and clicks buttons will
   eventually find a delete/pay/cancel button. Clicking it by default is not
   exploration, it's an incident.

## Decision

1. **State identity is `(path, structural fingerprint)`, not bare URL.** The
   fingerprint (`modules/explorer/interact.py::fingerprint`) hashes the normalized path
   plus a sorted signature of each interactive element's `tag`/`type`/identity
   attribute/`role` — deliberately excluding free text and field *values*, so two loads
   of the same logical state hash identically. A `StateNode`-derived graph
   (`InteractionGraph`) keys nodes by this pair instead of by URL, so a same-URL state
   change is a distinct node.

   Filling a field does not change this fingerprint by design (its structure hasn't
   changed, only its value) — so progress within one page visit is *not* tracked via
   the fingerprint, but via an explicit `attempted_selectors` set for that visit. This
   is what lets a multi-field form be filled field by field before a fingerprint-visible
   state change (a submit that actually succeeds) occurs, without either re-trying an
   already-filled field forever or stopping after the first one.

2. **Actions are a closed, schema-constrained document, never DOM-derived free text.**
   `modules/explorer/actions.py::Action` is `{type: click|fill|select|submit, target:
   ElementRef, value}`, where `target` always points at an element code already
   extracted (`modules/explorer/elements.py`). Nothing here is a selector or value
   built ad hoc from page content at decision time. This extends ADR-0004 point 3 to
   the explorer: the DOM may inform which of the *already-extracted* candidates ranks
   highest, but it can never expand the vocabulary of what an action is.

3. **Ranking is rules-first; a model arbitrates only within the closed set.** Mirrors
   `modules/triage/classifier.py` + `triage/agent.py` exactly: `enumerate_actions`
   scores every safe candidate deterministically (testid, CTA text, form membership,
   required-ness, position), and `arbitrate_action` is consulted only when the top
   score is ambiguous (`ARBITRATION_THRESHOLD = 0.55`) and a real provider is
   configured. Its schema is `{chosen_index: int, reason: str}` — an index into the
   already-ranked list. An out-of-range index, malformed response, unavailable
   provider, or exhausted budget all fall back to the heuristic's own top pick, so the
   explorer produces a useful result with zero model calls, the same invariant every
   other feature in QAgent holds.

4. **Destructive actions are opt-in, not opt-out.** `InteractionPolicy` drops any
   element whose text/label/name matches a destructive-keyword list (delete, pay,
   cancel, checkout, ...) before it is ever scored — not merely ranked low — unless
   `allow_destructive=True` is passed explicitly (`--allow-destructive` on the CLI).
   Form fields are only ever filled with synthetic values (`fill_fake_data`, on by
   default), never real data.

5. **A failed *action* is never treated as a defect; an action-triggered exception
   always is.** An element that couldn't be found or clicked is evidence about the
   heuristic's selector, not the application — reporting it as a bug would reintroduce
   exactly the selector-based flakiness ADR-0002 kept out of the API layer. An uncaught
   JS exception or console error while an action ran, however, is the same
   unambiguous evidence class `modules/browser/triage.py` already uses for a page load
   (`classify_action_outcome`, mirroring `classify_page_check`) — so that alone
   produces a `real_bug` verdict and a bug report, regardless of which element
   triggered it.

6. **Bounded by construction, not by luck.** `max_pages`, `max_depth`,
   `max_total_actions`, and `max_actions_per_page` cap the crawl the same way
   `crawler.explore` already caps a link-only one. Because progress within a page visit
   is tracked by attempted selectors rather than by re-deriving it from the
   fingerprint, the candidate list for a given page strictly shrinks every iteration
   until it's empty — the loop cannot spin on a no-op action.

## Consequences

- `modules/explorer/crawler.py` is untouched except two additive, defaulted fields on
  `StateNode` (`dom_fingerprint`, `actions_available`); the plain link-crawl code path
  and every test depending on it are unaffected.
- `Environment.interactive_exploration_enabled` (default `False`, same shape as
  `e2e_enabled`) gates this in the persisted pipeline; existing environments do not
  start clicking things on upgrade.
- A form with more required fields than `max_actions_per_page` can rank some of them
  out of the candidate list before the "defer submit" rule ever sees them. Accepted for
  v1 — the cap exists to bound noise, and the failure mode is "submits an incomplete
  form," not a crash or a false defect report.
- Interaction traces are stored in `TestResult.response`'s existing JSON column
  (`{"actions": [...]}`) rather than a new table; revisit only if that blob proves too
  large or unqueryable in practice.
