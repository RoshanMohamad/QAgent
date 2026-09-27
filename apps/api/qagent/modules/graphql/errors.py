"""Classify a GraphQL response's ``errors`` array.

GraphQL has no status code for "the resolver crashed". This module decides it,
and returns one of four kinds:

- ``none``      - no errors at all.
- ``handled``   - every error is one the API raised on purpose: it carries a
                  client-side code (Apollo's ``BAD_USER_INPUT``, ``NOT_FOUND``,
                  ``UNAUTHENTICATED`` ... or GitHub's top-level ``type``), or
                  its message reads as a deliberate client error.
- ``internal``  - at least one error is an unhandled exception: Apollo's
                  ``INTERNAL_SERVER_ERROR`` code, a leaked exception/stack
                  trace in ``extensions``, or a message that is a raw runtime
                  error ("invalid literal for int()", "Cannot read properties
                  of undefined").
- ``unclear``   - errors exist and none of the above can be said with
                  confidence.

``unclear`` exists so the conservative answer is available. False positives
are the primary metric (ADR-0003): a codeless, plain-English error from a
server that simply doesn't use codes is not evidence of a crash, and the
classifier treats ``unclear`` as nothing it can report.

Standalone on purpose - it imports only ``re`` - because the pytest emitter
embeds this file's source verbatim into generated suites, which must run
without QAgent installed. One implementation, not a copy that can drift.
"""

from __future__ import annotations

import re

INTERNAL_CODES = {"INTERNAL_SERVER_ERROR", "INTERNAL_ERROR", "INTERNAL"}

_RAW_EXCEPTION = re.compile(
    r"(invalid literal for int\(\)"
    r"|'NoneType' object"
    r"|object is not (subscriptable|iterable|callable)"
    r"|object has no attribute"
    r"|can only join an iterable"
    r"|list index out of range"
    r"|\b(KeyError|TypeError|ValueError|AttributeError|ZeroDivisionError)\b"
    r"|Cannot read propert(y|ies) of (undefined|null)"
    r"|is not a function"
    r"|undefined is not an object"
    r"|NullPointerException"
    r"|nil pointer dereference"
    r"|Traceback \(most recent call last\))",
    re.IGNORECASE,
)

_CLIENT_HINT = re.compile(
    r"(not found|does not exist|no such|invalid|must be|must not|required"
    r"|unauthori[sz]ed|unauthenticated|forbidden|permission|not allowed|expected)",
    re.IGNORECASE,
)


def _error_kind(error: object) -> str:
    if not isinstance(error, dict):
        return "unclear"
    extensions = error.get("extensions") if isinstance(error.get("extensions"), dict) else {}
    code = extensions.get("code") or error.get("type")
    message = str(error.get("message") or "")

    if isinstance(code, str) and code.upper() in INTERNAL_CODES:
        return "internal"
    if extensions.get("exception") or extensions.get("stacktrace"):
        return "internal"
    if _RAW_EXCEPTION.search(message):
        return "internal"
    if isinstance(code, str) and code:
        return "handled"
    if _CLIENT_HINT.search(message):
        return "handled"
    return "unclear"


def graphql_error_kind(body_json: object) -> str:
    """``none`` | ``handled`` | ``internal`` | ``unclear`` for one response body."""
    if not isinstance(body_json, dict):
        return "unclear"
    errors = body_json.get("errors")
    if not errors:
        return "none"
    if not isinstance(errors, list):
        return "unclear"
    kinds = {_error_kind(e) for e in errors}
    if "internal" in kinds:
        return "internal"
    if kinds == {"handled"}:
        return "handled"
    return "unclear"
