"""Assertion evaluation.

Assertions are data, not code. Nothing here evaluates a user-supplied expression, so
a generated (or model-suggested) test can never execute arbitrary logic inside the
platform. Each evaluator returns a structured outcome so the report can explain
precisely what was expected and what arrived.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass
class AssertionOutcome:
    type: str
    passed: bool
    expected: Any
    actual: Any
    message: str

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "passed": self.passed,
            "expected": self.expected,
            "actual": self.actual,
            "message": self.message,
        }


def _json_path(payload: Any, path: str) -> tuple[bool, Any]:
    """Resolve a dotted path with numeric list indices, e.g. items.0.id."""
    node = payload
    for part in [p for p in path.split(".") if p]:
        if isinstance(node, dict):
            if part not in node:
                return False, None
            node = node[part]
        elif isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return False, None
        else:
            return False, None
    return True, node


def evaluate(assertion: dict, response: dict) -> AssertionOutcome:
    """Evaluate one assertion against a captured response."""
    kind = assertion.get("type", "unknown")
    expected = assertion.get("value")
    status = response.get("status")
    body_text = response.get("body_text") or ""
    body_json = response.get("body_json")

    if kind == "status_in":
        passed = status in (expected or [])
        return AssertionOutcome(
            kind,
            passed,
            expected,
            status,
            f"expected status in {expected}, got {status}",
        )

    if kind == "status_not_in":
        passed = status not in (expected or [])
        return AssertionOutcome(
            kind,
            passed,
            f"not in {expected}",
            status,
            f"status {status} must not be one of {expected}",
        )

    if kind == "body_matches":
        passed = bool(re.search(expected or "", body_text, re.IGNORECASE))
        return AssertionOutcome(
            kind,
            passed,
            expected,
            _excerpt(body_text),
            f"body should match /{expected}/",
        )

    if kind == "body_not_matches":
        match = re.search(expected or "", body_text, re.IGNORECASE)
        return AssertionOutcome(
            kind,
            match is None,
            f"no match for {expected}",
            match.group(0) if match else None,
            f"body must not match /{expected}/",
        )

    if kind == "json_path_exists":
        found, value = _json_path(body_json, expected or "")
        return AssertionOutcome(
            kind,
            found,
            f"path {expected} present",
            value,
            f"response should contain path '{expected}'",
        )

    if kind == "json_path_equals":
        path = assertion.get("path", "")
        found, value = _json_path(body_json, path)
        return AssertionOutcome(
            kind,
            found and value == expected,
            {path: expected},
            value,
            f"'{path}' should equal {expected!r}",
        )

    if kind == "graphql_error_kind_in":
        # GraphQL answers a crashed resolver with 200 OK; the verdict lives in
        # the errors array (modules/graphql/errors.py).
        from qagent.modules.graphql.errors import graphql_error_kind

        actual = graphql_error_kind(body_json)
        first_error = None
        if isinstance(body_json, dict) and isinstance(body_json.get("errors"), list):
            first_error = (body_json["errors"] or [{}])[0]
        message = first_error.get("message") if isinstance(first_error, dict) else None
        return AssertionOutcome(
            kind,
            actual in (expected or []),
            expected,
            actual,
            f"graphql errors should be {expected}, got {actual}"
            + (f": {message}" if message else ""),
        )

    if kind == "graphql_field_denied":
        from qagent.modules.graphql.errors import graphql_field_denied

        field = str(expected or "")
        denied = graphql_field_denied(status, body_json, field)
        served = None
        if not denied and isinstance(body_json, dict):
            served = (body_json.get("data") or {}).get(field)
        return AssertionOutcome(
            kind,
            denied,
            f"no data for '{field}' without valid credentials",
            _excerpt(str(served)) if served is not None else status,
            f"'{field}' must not be served without valid credentials"
            + ("" if denied else f", but returned {_excerpt(str(served), 80)}"),
        )

    if kind == "header_present":
        headers = {k.lower(): v for k, v in (response.get("headers") or {}).items()}
        name = str(expected or "").lower()
        return AssertionOutcome(
            kind,
            name in headers,
            expected,
            list(headers),
            f"response should carry header '{expected}'",
        )

    if kind == "latency_under_ms":
        actual = response.get("duration_ms", 0)
        return AssertionOutcome(
            kind,
            actual <= (expected or 0),
            expected,
            actual,
            f"response should arrive within {expected}ms, took {actual}ms",
        )

    return AssertionOutcome(kind, False, expected, None, f"unknown assertion type '{kind}'")


def _excerpt(text: str, limit: int = 200) -> str:
    text = text.strip().replace("\n", " ")
    return text[:limit] + ("..." if len(text) > limit else "")


def evaluate_all(assertions: list[dict], response: dict) -> tuple[bool, list[dict]]:
    outcomes = [evaluate(a, response) for a in assertions]
    return all(o.passed for o in outcomes), [o.to_dict() for o in outcomes]
