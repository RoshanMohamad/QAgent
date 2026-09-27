"""Generation rules for GraphQL fields (modules/graphql/).

The REST rules do not transfer, and pretending they do would produce checks
that test the GraphQL library rather than the application:

- **Missing required field / wrong type.** GraphQL's own validation layer
  rejects these before any resolver runs. A check for them measures
  graphql-core or Apollo, not the code under test.
- **Status codes.** A resolver crash is usually ``200 OK`` with an ``errors``
  array. Every rule here asserts on ``graphql_error_kind_in`` instead
  (modules/graphql/errors.py), while keeping ``NO_SERVER_ERROR`` and
  ``NO_STACK_TRACE`` for the servers that *do* answer 500 or leak a trace.

What does transfer is the category of defect: an identifier the resolver
casts without guarding, a resource it assumes exists, and one GraphQL adds - an
optional argument sent as an explicit ``null``. GraphQL distinguishes "omitted"
from "null" (the spec makes both valid for a nullable argument), and resolvers
that handle the first with a default routinely crash on the second.

Nothing here generates an authentication check, and that is a deliberate gap
rather than an oversight: OpenAPI declares which operations are protected;
GraphQL introspection does not expose applied directives, so there is no
specification to hold a field to. Guessing would put false positives on every
legitimately public field.
"""

from __future__ import annotations

from typing import Any

from qagent.modules.discovery.openapi import EndpointSpec
from qagent.modules.generator.rules import NO_SERVER_ERROR, NO_STACK_TRACE, GeneratedCase
from qagent.modules.graphql.introspection import unwrap

#: A resolver may answer these with data, null, or a handled error - all fine.
#: Only an unhandled exception ("internal") or an unreadable one fails.
_HANDLED_OK = {"type": "graphql_error_kind_in", "value": ["none", "handled"]}
_NO_ERRORS = {"type": "graphql_error_kind_in", "value": ["none"]}

MALFORMED_ID = "not-a-number"
ABSENT_ID = "999999999"


def _example(base: str | None, kind: str | None, name: str, gql: dict, depth: int = 0) -> Any:
    if kind == "ENUM":
        return ((gql.get("enums") or {}).get(base or "") or ["QAGENT"])[0]
    if kind == "INPUT_OBJECT" and depth < 4:
        out = {}
        input_type = (gql.get("input_types") or {}).get(base or "") or {}
        for field in input_type.get("inputFields") or []:
            named, required, is_list = unwrap(field.get("type"))
            if required and field.get("defaultValue") is None:
                value = _example(
                    named.get("name"), named.get("kind"), field["name"], gql, depth + 1
                )
                out[field["name"]] = [value] if is_list else value
        return out
    if base == "ID":
        return "1"
    if base == "Int":
        return 1
    if base == "Float":
        return 1.0
    if base == "Boolean":
        return True
    return f"qagent-{name}"


def _arg_value(arg: dict, gql: dict) -> Any:
    value = _example(arg.get("base"), arg.get("kind"), arg["name"], gql)
    return [value] if arg.get("list") else value


def _document(gql: dict, variables: dict[str, Any]) -> str:
    args = [a for a in gql["args"] if a["name"] in variables]
    defs = ", ".join(f"${a['name']}: {a['type']}" for a in args)
    call = ", ".join(f"{a['name']}: ${a['name']}" for a in args)
    head = f"{gql['operation']} QAgent" + (f"({defs})" if defs else "")
    field = gql["field"] + (f"({call})" if call else "")
    selection = f" {gql['selection']}" if gql.get("selection") else ""
    return f"{head} {{ {field}{selection} }}"


def _case(
    endpoint: EndpointSpec,
    *,
    name: str,
    rationale: str,
    variables: dict[str, Any],
    negative: bool,
    expectation: str,
) -> GeneratedCase:
    gql = endpoint.graphql or {}
    return GeneratedCase(
        name=name,
        kind="api_functional",
        endpoint_key=endpoint.key(),
        rationale=rationale,
        spec={
            "request": {
                "method": "POST",
                "path": gql["endpoint"],
                "path_params": {},
                "query": {},
                "json": {"query": _document(gql, variables), "variables": variables},
                "auth": "default",
            },
            "assertions": [
                NO_SERVER_ERROR,
                _HANDLED_OK if negative else _NO_ERRORS,
                NO_STACK_TRACE,
            ],
            "expectation": expectation,
            # Read by triage (classifier.extract_signals): a GraphQL check has no
            # 4xx status_in to mark it as deliberately adversarial.
            "graphql": True,
            "negative": negative,
        },
    )


def _required_values(endpoint: EndpointSpec) -> dict[str, Any]:
    gql = endpoint.graphql or {}
    return {a["name"]: _arg_value(a, gql) for a in gql.get("args", []) if a["required"]}


def _first_id_arg(endpoint: EndpointSpec) -> dict | None:
    args = (endpoint.graphql or {}).get("args", [])
    return next((a for a in args if a["base"] == "ID" and not a["list"]), None)


def graphql_happy_path(endpoint: EndpointSpec) -> GeneratedCase:
    """Required arguments only, optional ones omitted - so this is also the
    check that catches a resolver dereferencing an optional argument it
    assumes was sent."""
    return _case(
        endpoint,
        name=f"{endpoint.key()} succeeds for a valid request",
        rationale="Baseline: a well-formed operation must resolve without errors.",
        variables=_required_values(endpoint),
        negative=False,
        expectation="A well-formed operation returns data and no errors.",
    )


def graphql_malformed_id(endpoint: EndpointSpec) -> GeneratedCase | None:
    """GraphQL's ``ID`` scalar accepts any string, so a non-numeric id passes
    validation and reaches the resolver - exactly where an unguarded cast lives.
    Null data or a handled error both pass; only a crash fails."""
    arg = _first_id_arg(endpoint)
    if arg is None:
        return None
    variables = {**_required_values(endpoint), arg["name"]: MALFORMED_ID}
    return _case(
        endpoint,
        name=f"{endpoint.key()} rejects a malformed '{arg['name']}'",
        rationale="Identifiers are routinely cast without guarding.",
        variables=variables,
        negative=True,
        expectation=f"A malformed '{arg['name']}' yields null or a handled error, never a crash.",
    )


def graphql_absent_id(endpoint: EndpointSpec) -> GeneratedCase | None:
    arg = _first_id_arg(endpoint)
    if arg is None:
        return None
    variables = {**_required_values(endpoint), arg["name"]: ABSENT_ID}
    return _case(
        endpoint,
        name=f"{endpoint.key()} handles an absent '{arg['name']}'",
        rationale="A well-formed identifier for a row that does not exist must not crash.",
        variables=variables,
        negative=True,
        expectation=f"An absent '{arg['name']}' yields null or a handled error, never a crash.",
    )


def graphql_explicit_null(endpoint: EndpointSpec) -> GeneratedCase | None:
    """Send ``null`` for the first optional argument. The GraphQL spec makes an
    explicit null valid for any nullable argument, and it reaches the resolver
    as a present-but-None value rather than as an absent one."""
    arg = next((a for a in (endpoint.graphql or {}).get("args", []) if not a["required"]), None)
    if arg is None:
        return None
    variables = {**_required_values(endpoint), arg["name"]: None}
    return _case(
        endpoint,
        name=f"{endpoint.key()} handles an explicit null '{arg['name']}'",
        rationale=(
            f"'{arg['name']}' is nullable, so null is valid input - distinct from omitting "
            "it, and a resolver that only defaults the omitted case crashes on it."
        ),
        variables=variables,
        negative=True,
        expectation=f"An explicit null '{arg['name']}' resolves or is handled, never a crash.",
    )


GRAPHQL_RULES = [
    graphql_happy_path,
    graphql_malformed_id,
    graphql_absent_id,
    graphql_explicit_null,
]
