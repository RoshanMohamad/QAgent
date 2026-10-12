"""Example value synthesis from JSON schema fragments.

Values are deterministic: the same schema always yields the same payload. That is a
requirement, not a convenience. If generated payloads drift between runs, a test that
starts failing cannot be attributed to a code change.
"""

from __future__ import annotations

from typing import Any

_FORMAT_EXAMPLES: dict[str, Any] = {
    "email": "qa.probe@example.com",
    "uri": "https://example.com/qagent",
    "url": "https://example.com/qagent",
    "uuid": "00000000-0000-4000-8000-000000000000",
    "date": "2026-01-01",
    "date-time": "2026-01-01T00:00:00Z",
    "password": "QAgentProbe!2026",
    "hostname": "example.com",
    "ipv4": "192.0.2.1",
}

#: A value of the wrong type for each JSON type, used to build negative cases.
_TYPE_VIOLATIONS: dict[str, Any] = {
    "string": 12345,
    "integer": "not-an-integer",
    "number": "not-a-number",
    "boolean": "not-a-boolean",
    "array": {"unexpected": "object"},
    "object": ["unexpected", "array"],
}


def example_for(schema: dict | None, name: str = "value", depth: int = 0) -> Any:
    """Build a plausible valid value for a schema fragment."""
    if not isinstance(schema, dict) or depth > 5:
        return "qagent"

    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]

    for combinator in ("oneOf", "anyOf", "allOf"):
        if schema.get(combinator):
            return example_for(schema[combinator][0], name, depth + 1)

    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "string")

    if kind == "object" or ("properties" in schema and not kind):
        out: dict[str, Any] = {}
        props = schema.get("properties", {})
        required = schema.get("required", list(props)[:3])
        for key in required:
            out[key] = example_for(props.get(key, {}), key, depth + 1)
        return out

    if kind == "array":
        return [example_for(schema.get("items", {}), name, depth + 1)]

    if kind == "integer":
        return int(schema.get("minimum", 1) or 1)
    if kind == "number":
        return float(schema.get("minimum", 1) or 1)
    if kind == "boolean":
        return True
    if kind == "null":
        return None

    fmt = schema.get("format")
    if fmt in _FORMAT_EXAMPLES:
        return _FORMAT_EXAMPLES[fmt]

    lowered = name.lower()
    for hint, value in _FORMAT_EXAMPLES.items():
        if hint in lowered:
            return value

    min_len = int(schema.get("minLength", 0) or 0)
    base = f"qagent-{name}"
    return base if len(base) >= min_len else base + "x" * (min_len - len(base))


def violation_for(schema: dict | None, name: str = "value") -> Any:
    """Build a value of the wrong type for a schema fragment."""
    if not isinstance(schema, dict):
        return {"unexpected": "object"}

    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "string")
    if kind in _TYPE_VIOLATIONS:
        return _TYPE_VIOLATIONS[kind]
    if "enum" in schema:
        return "qagent-not-in-enum"
    return {"unexpected": "object"}


def query_violation(schema: dict | None) -> str | None:
    """A query-string value of the wrong type, or ``None`` if none exists.

    Narrower than `violation_for` because a query string is text: ``12345`` is
    a perfectly good string, so a string parameter has no wrong *type* to send.
    Only parameters a handler has to parse - numbers, booleans - or match
    against a closed set - enums - can be violated this way.
    """
    if not isinstance(schema, dict):
        return None
    if "enum" in schema:
        return "qagent-not-in-enum"
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), None)
    if kind in {"integer", "number", "boolean"}:
        return str(_TYPE_VIOLATIONS[kind])
    return None


def example_body(schema: dict | None) -> dict:
    value = example_for(schema, "body")
    return value if isinstance(value, dict) else {"value": value}


def path_param_value(param: dict, *, valid: bool = True) -> str:
    """Produce a path segment value.

    The invalid variant is the single most productive negative case in practice:
    handlers routinely cast an identifier without guarding, turning a malformed id
    into a 500 where a 400 or 404 was specified.
    """
    schema = param.get("schema", {}) or {}
    kind = schema.get("type", "string")

    if valid:
        value = example_for(schema, param.get("name", "id"))
        return str(value)

    if kind in {"integer", "number"}:
        return "not-a-number"
    if schema.get("format") == "uuid":
        return "not-a-uuid"
    return "qagent-nonexistent-value"


def absent_resource_id(param: dict) -> str:
    """An id that is well-formed but should not exist."""
    schema = param.get("schema", {}) or {}
    kind = schema.get("type", "string")
    if kind in {"integer", "number"}:
        return "999999999"
    if schema.get("format") == "uuid":
        return "00000000-0000-4000-8000-00000000dead"
    return "qagent-absent-99999"


#: Checked in this order: a field can decorate several bounds at once (a string
#: with both minLength and maxLength, say), and the smallest, most specific
#: violation - one unit past whichever edge is nearest - is the fairest test of
#: whether that exact edge is enforced.
def _bound_violation(schema: dict) -> tuple[Any, str] | None:
    """A value one unit past whichever bound `schema` declares, and which bound
    that was - or ``None`` when it declares none of them."""
    if "minimum" in schema and isinstance(schema["minimum"], (int, float)):
        return schema["minimum"] - 1, "minimum"
    if "maximum" in schema and isinstance(schema["maximum"], (int, float)):
        return schema["maximum"] + 1, "maximum"
    if "minLength" in schema and isinstance(schema["minLength"], int) and schema["minLength"] > 0:
        return "x" * (schema["minLength"] - 1), "minLength"
    if "maxLength" in schema and isinstance(schema["maxLength"], int):
        return "x" * (schema["maxLength"] + 1), "maxLength"
    return None


def first_bounded_field(properties: dict) -> tuple[str, Any, str] | None:
    """The first property (in declaration order) with a declared minimum,
    maximum, minLength or maxLength, a value one unit past it, and which bound
    it was - or ``None`` when nothing here declares one.

    Declaration order, not alphabetical: a schema author's first property is
    usually its most central one, the same reasoning `required_body_fields[0]`
    already applies elsewhere in this generator.
    """
    for name, field_schema in properties.items():
        if not isinstance(field_schema, dict):
            continue
        violation = _bound_violation(field_schema)
        if violation is not None:
            value, described = violation
            return name, value, described
    return None
