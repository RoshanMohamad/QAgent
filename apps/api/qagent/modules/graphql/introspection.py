"""Discover a GraphQL API by introspection.

Every root field of ``Query`` and ``Mutation`` becomes one ``EndpointSpec``,
the same unit of work an OpenAPI operation is. That is what lets planning,
generation, persistence, the dashboard and the evaluation harness handle
GraphQL without a second code path: they already key everything on
``endpoint.key()``, and a field's key is ``QUERY /graphql/book`` the way an
operation's is ``GET /books/{id}``.

The path puts the GraphQL endpoint first (``/graphql/book``) so the planner
groups every field of one API into one module, rather than inventing a module
per field name.

Only the operation document for a field is built here - its argument types
and a selection set of its return type's scalar fields, one level deep.
Deeper selections would reach more resolvers, but they would also turn every
check into a check of several resolvers at once, and a failure would no
longer name the field that caused it.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from qagent.modules.discovery.openapi import EndpointSpec, score_risk

logger = logging.getLogger(__name__)

#: Probed in order when no explicit GraphQL path is given.
COMMON_GRAPHQL_PATHS = ["/graphql", "/api/graphql", "/graphql/v1", "/v1/graphql", "/query"]

_TYPE_REF = """
fragment TypeRef on __Type {
  kind name
  ofType { kind name ofType { kind name ofType { kind name ofType { kind name } } } }
}
"""

INTROSPECTION_QUERY = (
    """
query QAgentIntrospection {
  __schema {
    queryType { name }
    mutationType { name }
    types {
      kind name
      fields(includeDeprecated: true) {
        name description
        args { name defaultValue type { ...TypeRef } }
        type { ...TypeRef }
      }
      inputFields { name defaultValue type { ...TypeRef } }
      enumValues(includeDeprecated: true) { name }
    }
  }
}
"""
    + _TYPE_REF
)


def unwrap(type_ref: dict | None) -> tuple[dict, bool, bool]:
    """(innermost named type, outermost is NON_NULL, a LIST appears anywhere)."""
    node = type_ref or {}
    required = node.get("kind") == "NON_NULL"
    is_list = False
    while node.get("kind") in {"NON_NULL", "LIST"}:
        if node.get("kind") == "LIST":
            is_list = True
        node = node.get("ofType") or {}
    return node, required, is_list


def type_literal(type_ref: dict | None) -> str:
    """Render a type reference back to SDL (``[String!]!``) for a variable definition."""
    node = type_ref or {}
    kind = node.get("kind")
    if kind == "NON_NULL":
        return f"{type_literal(node.get('ofType'))}!"
    if kind == "LIST":
        return f"[{type_literal(node.get('ofType'))}]"
    return str(node.get("name") or "String")


def _selection(return_ref: dict | None, types: dict[str, dict]) -> str:
    """The scalar and enum fields of an object return type, one level deep."""
    named, _, _ = unwrap(return_ref)
    if named.get("kind") not in {"OBJECT", "INTERFACE"}:
        return ""
    fields = []
    for field in (types.get(named.get("name") or "") or {}).get("fields") or []:
        leaf, _, _ = unwrap(field.get("type"))
        takes_required_args = any(
            unwrap(arg.get("type"))[1] and arg.get("defaultValue") is None
            for arg in field.get("args") or []
        )
        if leaf.get("kind") in {"SCALAR", "ENUM"} and not takes_required_args:
            fields.append(field["name"])
    return "{ " + " ".join(fields or ["__typename"]) + " }"


def parse_introspection(result: dict, *, graphql_path: str = "/graphql") -> list[EndpointSpec]:
    """Flatten an introspection result into one endpoint per root field.

    Never raises on a malformed result: an API that half-answers introspection
    yields the fields that did parse, same contract as ``parse_openapi``.
    """
    schema = ((result or {}).get("data") or {}).get("__schema") or {}
    types = {
        t["name"]: t
        for t in schema.get("types") or []
        if isinstance(t, dict) and t.get("name")
    }
    roots = [
        ("query", (schema.get("queryType") or {}).get("name")),
        ("mutation", (schema.get("mutationType") or {}).get("name")),
    ]
    prefix = "/" + graphql_path.strip("/")
    endpoints: list[EndpointSpec] = []

    for operation, type_name in roots:
        if not type_name or type_name not in types:
            continue
        for field in types[type_name].get("fields") or []:
            name = field.get("name")
            if not name or name.startswith("__"):
                continue
            try:
                args = []
                for arg in field.get("args") or []:
                    named, required, is_list = unwrap(arg.get("type"))
                    args.append(
                        {
                            "name": arg["name"],
                            "type": type_literal(arg.get("type")),
                            "base": named.get("name"),
                            "kind": named.get("kind"),
                            "required": required and arg.get("defaultValue") is None,
                            "list": is_list,
                        }
                    )
                path = f"{prefix}/{name}"
                # Risk as its REST twin would score: a mutation is a write, an
                # ID argument is an identifier - the same two signals score_risk
                # reads from a method and a `{param}` segment.
                has_id = any(a["base"] == "ID" for a in args)
                risk = score_risk(
                    "post" if operation == "mutation" else "get",
                    path + ("/{id}" if has_id else ""),
                    False,
                )
                endpoints.append(
                    EndpointSpec(
                        method=operation.upper(),
                        path=path,
                        operation_id=name,
                        summary=field.get("description"),
                        source="graphql",
                        risk_score=risk,
                        graphql={
                            "endpoint": prefix,
                            "operation": operation,
                            "field": name,
                            "args": args,
                            "selection": _selection(field.get("type"), types),
                            "input_types": {
                                t: types[t]
                                for t in _input_types_used(args, types)
                            },
                            "enums": {
                                t["name"]: [v["name"] for v in t.get("enumValues") or []]
                                for t in types.values()
                                if t.get("kind") == "ENUM" and not t["name"].startswith("__")
                            },
                        },
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one bad field must not stop discovery
                logger.warning("skipping graphql field %s: %s", name, exc)

    endpoints.sort(key=lambda e: (-e.risk_score, e.path, e.method))
    return endpoints


def _input_types_used(args: list[dict], types: dict[str, dict], seen: set | None = None) -> set:
    """Every INPUT_OBJECT an argument list reaches, so values can be built for them."""
    seen = seen if seen is not None else set()
    for arg in args:
        base = arg.get("base")
        if arg.get("kind") == "INPUT_OBJECT" and base and base not in seen and base in types:
            seen.add(base)
            nested = []
            for field in types[base].get("inputFields") or []:
                named, _, _ = unwrap(field.get("type"))
                nested.append({"base": named.get("name"), "kind": named.get("kind")})
            _input_types_used(nested, types, seen)
    return seen


def fetch_graphql(
    base_url: str,
    *,
    explicit_path: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> tuple[list[EndpointSpec], str | None]:
    """Probe for a GraphQL endpoint and introspect it.

    An API with introspection disabled (a common production hardening) is
    simply not discovered here - it is not an error, and there is no way to
    enumerate a GraphQL surface without it that would not amount to guessing
    field names.
    """
    paths = [explicit_path] if explicit_path else COMMON_GRAPHQL_PATHS
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for path in paths:
            url = base_url.rstrip("/") + "/" + path.strip("/")
            try:
                response = client.post(
                    url, json={"query": INTROSPECTION_QUERY}, headers=headers or {}
                )
            except httpx.HTTPError as exc:
                logger.debug("graphql probe failed %s: %s", url, exc)
                continue
            if response.status_code != 200:
                continue
            try:
                payload: Any = response.json()
            except ValueError:
                continue
            if not isinstance(payload, dict) or not (payload.get("data") or {}).get("__schema"):
                continue
            endpoints = parse_introspection(payload, graphql_path=path)
            if endpoints:
                logger.info("discovered graphql schema at %s (%d fields)", url, len(endpoints))
                return endpoints, url
    return [], None
