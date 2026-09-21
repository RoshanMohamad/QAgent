"""Convert a recorded browser session into tests (CLAUDE.md section 10).

The extension produces two parallel streams: what the user *did* (clicks,
fills, submits) and what the application *did about it* (XHR/fetch traffic).
The value is in the second one. A recorded click on "Checkout" is a UI script;
the `POST /api/orders` it triggered is an API test that runs in a second, needs
no browser, and does not break when the button moves.

So a session produces both:

* a **UI flow** - the ordered actions, ready for a Playwright emitter, and
* **API checks** - the requests the flow provoked, in exactly the declarative
  shape `modules/generator/rules.py` already produces, so they execute through
  the existing runner and get triaged by the existing classifier.

Everything arriving here is untrusted. The file came from a browser extension
running on a page the developer visited, which means an attacker-controlled
page could have shaped it. So the parser validates types, bounds every list,
and never evaluates anything.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlparse

logger = logging.getLogger(__name__)

#: Bounds on a session document. A recorder capped at 500 actions can still be
#: handed a hand-edited file claiming 5 million.
MAX_ACTIONS = 1_000
MAX_REQUESTS = 2_000

#: Actions the UI flow understands. Anything else is dropped with a warning
#: rather than passed through, so a malformed or hostile document cannot
#: introduce a step type the emitter has never seen.
KNOWN_ACTIONS = {"click", "fill", "select", "check", "submit"}

#: Diagnostics rather than steps: recorded, reported, never replayed.
DIAGNOSTIC_ACTIONS = {"console_error", "page_error"}

#: Requests worth turning into a check. A GET that merely rendered the page is
#: already covered by ordinary discovery; the writes are what a recorded flow
#: uniquely reveals, because they need a logged-in user doing a real sequence.
INTERESTING_METHODS = {"POST", "PUT", "PATCH", "DELETE", "GET"}

#: Never replayed. Replaying a logout mid-suite invalidates the session every
#: later check depends on, and the failure looks like an auth bug.
_DESTRUCTIVE_PATH_HINTS = ("logout", "signout", "sign-out", "revoke")


@dataclass
class RecordedAction:
    type: str
    selector: str | None = None
    value: str | None = None
    url: str | None = None
    tag: str | None = None
    text: str | None = None
    redacted: bool = False
    fragile: bool = False

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "selector": self.selector,
            "value": self.value,
            "url": self.url,
            "tag": self.tag,
            "text": self.text,
            "redacted": self.redacted,
            "fragile": self.fragile,
        }


@dataclass
class RecordedRequest:
    method: str
    url: str
    status: int | None = None
    #: The JSON body, when the extension captured one and it survived scrubbing.
    #: None means "not recorded", which is what decides whether this request can
    #: be replayed faithfully - see `to_api_cases`.
    body: Any = None
    query: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "url": self.url,
            "status": self.status,
            "body": self.body,
            "query": self.query,
        }


@dataclass
class RecordedSession:
    start_url: str | None = None
    actions: list[RecordedAction] = field(default_factory=list)
    requests: list[RecordedRequest] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def fragile_selectors(self) -> int:
        return sum(1 for a in self.actions if a.fragile)

    def summary(self) -> dict:
        return {
            "start_url": self.start_url,
            "actions": len(self.actions),
            "requests": len(self.requests),
            "diagnostics": len(self.diagnostics),
            "fragile_selectors": self.fragile_selectors,
            "warnings": self.warnings,
        }


def _text(value: Any, limit: int = 300) -> str | None:
    if value is None:
        return None
    return str(value)[:limit]


#: Body fields never kept. The first request in almost every recorded session
#: is a login, so a recorder that stores request bodies verbatim writes the
#: user's password into a file that then gets committed or pasted into an
#: issue. The key names are matched, not the values, because a password is
#: indistinguishable from any other string once it is out of context.
_SECRET_FIELD = re.compile(
    r"pass|secret|token|auth|cvv|card|ssn|otp|credential|api[-_]?key", re.IGNORECASE
)

#: Depth limit for scrubbing. A hand-edited session file could nest a million
#: objects; recursion without a bound is a stack overflow waiting to be sent.
_MAX_BODY_DEPTH = 8


def _scrub_body(value: Any, depth: int = 0) -> Any:
    """Redact secret-looking fields anywhere in a recorded request body."""
    if depth >= _MAX_BODY_DEPTH:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            key: ("[redacted]" if _SECRET_FIELD.search(str(key)) else _scrub_body(item, depth + 1))
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, list):
        return [_scrub_body(item, depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return value[:1000]
    return value


def parse_session(document: Any) -> RecordedSession:
    """Validate and normalise an exported session.

    Defensive by design: this reads a file produced on a page QAgent does not
    control, so every field is type-checked and every list is bounded. A
    malformed document degrades to a partial session with warnings rather than
    raising, because a recording someone spent ten minutes on should not be
    lost to one bad entry.
    """
    session = RecordedSession()

    if not isinstance(document, dict):
        session.warnings.append("session document is not an object; nothing was imported")
        return session

    session.start_url = _text(document.get("start_url"), 500)

    raw_actions = document.get("actions")
    if not isinstance(raw_actions, list):
        session.warnings.append("no action list found")
        raw_actions = []
    if len(raw_actions) > MAX_ACTIONS:
        session.warnings.append(f"truncated to the first {MAX_ACTIONS} actions")
        raw_actions = raw_actions[:MAX_ACTIONS]

    for entry in raw_actions:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("type", ""))

        if kind in DIAGNOSTIC_ACTIONS:
            message = _text(entry.get("message"), 500)
            if message:
                session.diagnostics.append(f"{kind}: {message}")
            continue

        if kind not in KNOWN_ACTIONS:
            session.warnings.append(f"ignored unknown action type {kind!r}")
            continue

        target = entry.get("target") if isinstance(entry.get("target"), dict) else {}
        selector = _text(target.get("selector"), 500)
        if not selector:
            session.warnings.append(f"ignored {kind} with no selector")
            continue

        session.actions.append(
            RecordedAction(
                type=kind,
                selector=selector,
                value=_text(entry.get("value")),
                url=_text(entry.get("url"), 500),
                tag=_text(target.get("tag"), 32),
                text=_text(target.get("text"), 120),
                redacted=bool(entry.get("redacted")),
                fragile=bool(target.get("fragile")),
            )
        )

    raw_requests = document.get("requests")
    if not isinstance(raw_requests, list):
        raw_requests = []
    if len(raw_requests) > MAX_REQUESTS:
        session.warnings.append(f"truncated to the first {MAX_REQUESTS} requests")
        raw_requests = raw_requests[:MAX_REQUESTS]

    for entry in raw_requests:
        if not isinstance(entry, dict):
            continue
        url = _text(entry.get("url"), 1000)
        method = str(entry.get("method", "GET")).upper()
        if not url or method not in INTERESTING_METHODS:
            continue
        status = entry.get("status")

        # Only a JSON object/array is kept. A multipart upload or a raw blob is
        # not something the declarative runner can replay, and storing it would
        # mean a generated check that silently sends the wrong content type.
        body = entry.get("body")
        if not isinstance(body, dict | list):
            body = None
        else:
            body = _scrub_body(body)

        session.requests.append(
            RecordedRequest(
                method=method,
                url=url,
                status=int(status) if isinstance(status, int) else None,
                body=body,
                query=dict(parse_qsl(urlparse(url).query)[:20]),
            )
        )

    if session.fragile_selectors:
        session.warnings.append(
            f"{session.fragile_selectors} action(s) recorded a positional selector; "
            "these break on the next CSS change - add a data-testid to those elements"
        )

    logger.info(
        "parsed session: %d action(s), %d request(s), %d warning(s)",
        len(session.actions),
        len(session.requests),
        len(session.warnings),
    )
    return session


def _is_destructive(path: str) -> bool:
    lowered = path.lower()
    return any(hint in lowered for hint in _DESTRUCTIVE_PATH_HINTS)


def to_api_cases(session: RecordedSession, *, base_url: str | None = None) -> list:
    """Turn the observed traffic into checks the existing runner can execute.

    This is the part that makes recording worth building. The requests a real
    logged-in flow produced are exactly the ones static discovery cannot reach:
    they need a session, a cart with something in it, an order that exists.

    Every case asserts what was actually observed - if the recording saw a 201,
    the check asserts a 2xx - plus the invariant every generated case carries:
    never a 5xx. A recorded request that returned 500 during recording becomes
    a check that fails immediately, which is correct: it already found a defect.
    """
    from qagent.modules.generator.rules import NO_SERVER_ERROR, NO_STACK_TRACE, GeneratedCase

    cases: list[GeneratedCase] = []
    seen: set[tuple[str, str]] = set()

    for request in session.requests:
        parsed = urlparse(request.url)
        path = parsed.path or "/"

        if base_url:
            # Only the application under test. A recorded session is full of
            # analytics and CDN traffic, and generating checks against a third
            # party's servers would be both useless and rude.
            origin = urlparse(base_url)
            if parsed.netloc and origin.netloc and parsed.netloc != origin.netloc:
                continue

        if _is_destructive(path):
            continue

        key = (request.method, path)
        if key in seen:
            continue
        seen.add(key)

        # Whether this request can be replayed *faithfully* decides what it is
        # allowed to assert. A POST whose body was not recorded will be replayed
        # with no body, so the application will correctly answer 422 - and a
        # check asserting 2xx would fail every run while the application is
        # working perfectly. That is a false positive, which is the one thing
        # this project optimises against (ADR-0003), so it is not generated.
        replayable = request.method == "GET" or request.body is not None

        assertions: list[dict] = []
        if replayable:
            expected = [200, 201, 202, 204]
            if request.status and 200 <= request.status < 400:
                expected = sorted({request.status, *expected})
            assertions.append({"type": "status_in", "value": expected})
            expectation = (
                "This request succeeded during recording"
                + (f" with {request.status}" if request.status else "")
                + "; it must keep succeeding."
            )
        else:
            expectation = (
                f"No request body was recorded for this {request.method}, so it cannot be "
                "replayed faithfully. It is still checked for the invariant that holds "
                "regardless of input: a malformed or empty request is a client error, "
                "never a server error."
            )

        assertions.extend([NO_SERVER_ERROR, NO_STACK_TRACE])

        suffix = "still works" if replayable else "never 5xxs"
        cases.append(
            GeneratedCase(
                name=f"{request.method} {path} {suffix} (recorded)",
                kind="api_functional",
                endpoint_key=f"{request.method} {path}",
                generated_by="recording",
                rationale=(
                    "Observed during a recorded user session, so it exercises a flow "
                    "static discovery cannot reach without credentials and state."
                ),
                spec={
                    "request": {
                        "method": request.method,
                        "path": path,
                        "path_params": {},
                        "query": dict(request.query or {}),
                        "json": request.body,
                        "auth": "default",
                    },
                    "assertions": assertions,
                    "expectation": expectation,
                },
            )
        )

    return cases


def to_ui_flow(session: RecordedSession) -> dict:
    """The ordered UI steps, as a declarative document.

    Same choice the generator makes: a document, not emitted code. It is
    diffable, reviewable, and can be rendered to Playwright later without
    re-recording anything.
    """
    return {
        "kind": "ui_flow",
        "start_url": session.start_url,
        "steps": [a.to_dict() for a in session.actions],
        "diagnostics": session.diagnostics,
        "warnings": session.warnings,
        "requires_secrets": [
            a.selector for a in session.actions if a.redacted and a.selector
        ],
    }
