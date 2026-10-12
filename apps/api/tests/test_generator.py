"""Generation and discovery tests."""

from __future__ import annotations

from qagent.modules.discovery.openapi import EndpointSpec, parse_openapi, score_risk
from qagent.modules.generator import values
from qagent.modules.generator.rules import (
    case_boundary_value,
    case_missing_required_query,
    case_wrong_query_type,
    generate,
)

DOCUMENT = {
    "openapi": "3.0.0",
    "info": {"title": "shop", "version": "1"},
    "components": {
        "schemas": {
            "NewOrder": {
                "type": "object",
                "required": ["product_id", "quantity"],
                "properties": {
                    "product_id": {"type": "integer"},
                    "quantity": {"type": "integer"},
                },
            }
        },
        "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
    },
    "paths": {
        "/products/{product_id}": {
            "get": {
                "operationId": "getProduct",
                "parameters": [
                    {
                        "name": "product_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer"},
                    }
                ],
                "responses": {"200": {"description": "ok"}, "404": {"description": "missing"}},
            }
        },
        "/orders": {
            "post": {
                "operationId": "createOrder",
                "security": [{"bearer": []}],
                "requestBody": {
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/NewOrder"}}
                    }
                },
                "responses": {"201": {"description": "created"}, "400": {"description": "bad"}},
            }
        },
    },
}


class TestDiscovery:
    def test_operations_are_flattened(self):
        endpoints = parse_openapi(DOCUMENT)
        assert {e.key() for e in endpoints} == {
            "GET /products/{product_id}",
            "POST /orders",
        }

    def test_local_refs_are_resolved(self):
        order = next(e for e in parse_openapi(DOCUMENT) if e.method == "POST")
        assert order.required_body_fields == ["product_id", "quantity"]

    def test_security_is_detected(self):
        order = next(e for e in parse_openapi(DOCUMENT) if e.method == "POST")
        assert order.requires_auth is True

    def test_success_status_comes_from_the_contract(self):
        order = next(e for e in parse_openapi(DOCUMENT) if e.method == "POST")
        assert order.success_status == 201

    def test_malformed_document_does_not_raise(self):
        assert parse_openapi({"paths": "not-an-object"}) == []

    def test_risk_ranks_sensitive_writes_above_reads(self):
        assert score_risk("delete", "/admin/users/{id}", True) > score_risk("get", "/health", False)

    def test_endpoints_are_returned_highest_risk_first(self):
        endpoints = parse_openapi(DOCUMENT)
        assert endpoints == sorted(endpoints, key=lambda e: (-e.risk_score, e.path, e.method))


class TestGeneration:
    def test_every_endpoint_gets_a_happy_path(self):
        cases = generate(parse_openapi(DOCUMENT)).cases
        happy = [c for c in cases if "valid request" in c.name]
        assert len(happy) == 2

    def test_required_field_omission_is_generated(self):
        cases = generate(parse_openapi(DOCUMENT)).cases
        assert any("missing 'product_id'" in c.name for c in cases)

    def test_protected_endpoints_get_an_auth_probe(self):
        cases = generate(parse_openapi(DOCUMENT)).cases
        probes = [c for c in cases if c.spec["request"].get("auth") == "none"]
        assert probes
        assert all(c.kind == "api_security" for c in probes)

    def test_negative_cases_forbid_server_errors(self):
        cases = generate(parse_openapi(DOCUMENT)).cases
        negative = [c for c in cases if "rejects" in c.name]
        assert negative
        for case in negative:
            types = {a["type"] for a in case.spec["assertions"]}
            assert "status_not_in" in types

    def test_generation_is_deterministic(self):
        endpoints = parse_openapi(DOCUMENT)
        first = [c.to_dict() for c in generate(endpoints).cases]
        second = [c.to_dict() for c in generate(endpoints).cases]
        assert first == second

    def test_case_limit_is_respected(self):
        assert len(generate(parse_openapi(DOCUMENT), max_cases=3).cases) <= 3

    def test_endpoint_without_parameters_skips_path_rules(self):
        endpoint = EndpointSpec(method="GET", path="/health", responses={"200": {}})
        names = {c.name for c in generate([endpoint]).cases}
        assert not any("malformed" in n for n in names)

    def test_no_boundary_case_when_the_schema_declares_no_bound(self):
        # NewOrder's fields carry no minimum/maximum/length constraint.
        cases = generate(parse_openapi(DOCUMENT)).cases
        assert not any("past its" in c.name for c in cases)


class TestBoundaryValueRule:
    """CLAUDE.md section 12's "Boundary values" - `case_boundary_value`."""

    document = {
        "openapi": "3.0.0",
        "info": {"title": "shop", "version": "1"},
        "paths": {
            "/orders": {
                "post": {
                    "operationId": "createOrder",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["quantity"],
                                    "properties": {
                                        "quantity": {
                                            "type": "integer",
                                            "minimum": 1,
                                            "maximum": 100,
                                        },
                                    },
                                }
                            }
                        }
                    },
                    "responses": {"201": {}, "400": {}},
                }
            }
        },
    }

    def _endpoint(self):
        return next(iter(parse_openapi(self.document)))

    def test_fires_when_a_bound_is_declared(self):
        case = case_boundary_value(self._endpoint())
        assert case is not None
        assert case.kind == "api_functional"

    def test_sends_the_value_one_unit_past_the_nearer_bound(self):
        # minimum (1) is checked before maximum (100) in first_bounded_field -
        # the violation is one below it, not one above the maximum.
        case = case_boundary_value(self._endpoint())
        assert case.spec["request"]["json"]["quantity"] == 0

    def test_asserts_a_4xx_and_no_server_error(self):
        case = case_boundary_value(self._endpoint())
        types = {a["type"] for a in case.spec["assertions"]}
        assert types == {"status_in", "status_not_in", "body_not_matches"}
        status_in = next(a for a in case.spec["assertions"] if a["type"] == "status_in")
        assert status_in["value"] == [400, 409, 422]

    def test_none_when_the_endpoint_has_no_request_body(self):
        endpoint = EndpointSpec(method="GET", path="/health", responses={"200": {}})
        assert case_boundary_value(endpoint) is None

    def test_none_when_no_property_declares_a_bound(self):
        document = {
            "openapi": "3.0.0",
            "info": {"title": "x", "version": "1"},
            "paths": {
                "/things": {
                    "post": {
                        "operationId": "create",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"name": {"type": "string"}},
                                    }
                                }
                            }
                        },
                        "responses": {"201": {}},
                    }
                }
            },
        }
        endpoint = next(iter(parse_openapi(document)))
        assert case_boundary_value(endpoint) is None

    def test_generated_deterministically_like_every_other_rule(self):
        endpoints = parse_openapi(self.document)
        first = case_boundary_value(endpoints[0]).to_dict()
        second = case_boundary_value(endpoints[0]).to_dict()
        assert first == second


class TestBoundaryValueHelper:
    """qagent.modules.generator.values.first_bounded_field"""

    def test_prefers_minimum_over_maximum_on_the_same_field(self):
        found = values.first_bounded_field({"n": {"type": "integer", "minimum": 5, "maximum": 10}})
        assert found == ("n", 4, "minimum")

    def test_falls_back_to_maximum_when_no_minimum_is_declared(self):
        found = values.first_bounded_field({"n": {"type": "integer", "maximum": 10}})
        assert found == ("n", 11, "maximum")

    def test_min_length_one_produces_an_empty_string(self):
        found = values.first_bounded_field({"s": {"type": "string", "minLength": 1}})
        assert found == ("s", "", "minLength")

    def test_max_length_produces_one_character_too_many(self):
        found = values.first_bounded_field({"s": {"type": "string", "maxLength": 3}})
        assert found == ("s", "xxxx", "maxLength")

    def test_returns_the_first_bounded_property_in_declaration_order(self):
        found = values.first_bounded_field(
            {
                "unbounded": {"type": "string"},
                "bounded": {"type": "integer", "minimum": 0},
                "also_bounded": {"type": "integer", "minimum": 0},
            }
        )
        assert found[0] == "bounded"

    def test_none_when_nothing_declares_a_bound(self):
        assert values.first_bounded_field({"n": {"type": "integer"}, "s": {"type": "string"}}) is None

    def test_non_numeric_bound_values_are_ignored_rather_than_crashing(self):
        # A malformed spec (a string where a number belongs) must not raise.
        assert values.first_bounded_field({"n": {"type": "integer", "minimum": "not-a-number"}}) is None


def _query_endpoint(*params: dict) -> EndpointSpec:
    return EndpointSpec(
        method="GET",
        path="/items",
        parameters=[{"in": "query", **p} for p in params],
        responses={"200": {}, "400": {}},
    )


class TestQueryParameterRules:
    """`case_missing_required_query` and `case_wrong_query_type`: the first
    rules that vary the query string (inventory-api's BUG-501 and BUG-502)."""

    def test_missing_required_query_drops_only_the_first_required_one(self):
        endpoint = _query_endpoint(
            {"name": "q", "required": True, "schema": {"type": "string"}},
            {"name": "region", "required": True, "schema": {"type": "string"}},
            {"name": "page", "required": False, "schema": {"type": "integer"}},
        )
        case = case_missing_required_query(endpoint)
        query = case.spec["request"]["query"]
        assert "q" not in query
        assert "region" in query  # the other required one is still sent
        assert "page" not in query  # optional ones never are

    def test_missing_required_query_needs_a_required_parameter(self):
        endpoint = _query_endpoint({"name": "page", "required": False, "schema": {"type": "integer"}})
        assert case_missing_required_query(endpoint) is None

    def test_wrong_query_type_targets_optional_typed_parameters_too(self):
        endpoint = _query_endpoint(
            {"name": "q", "required": True, "schema": {"type": "string"}},
            {"name": "page", "required": False, "schema": {"type": "integer"}},
        )
        case = case_wrong_query_type(endpoint)
        assert case.spec["request"]["query"] == {"page": "not-an-integer", "q": "qagent-q"}

    def test_wrong_query_type_skips_parameters_that_have_no_wrong_type(self):
        endpoint = _query_endpoint({"name": "q", "required": True, "schema": {"type": "string"}})
        assert case_wrong_query_type(endpoint) is None

    def test_both_assert_a_400_and_no_server_error(self):
        endpoint = _query_endpoint(
            {"name": "page", "required": True, "schema": {"type": "integer"}}
        )
        for case in (case_missing_required_query(endpoint), case_wrong_query_type(endpoint)):
            status_in = next(a for a in case.spec["assertions"] if a["type"] == "status_in")
            assert status_in["value"] == [400, 422]
            assert any(a["type"] == "status_not_in" for a in case.spec["assertions"])

    def test_path_parameters_are_not_query_parameters(self):
        endpoint = EndpointSpec(
            method="GET",
            path="/items/{id}",
            parameters=[{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            responses={"200": {}},
        )
        assert case_missing_required_query(endpoint) is None
        assert case_wrong_query_type(endpoint) is None

    def test_planner_names_them(self):
        endpoint = _query_endpoint({"name": "page", "required": True, "schema": {"type": "integer"}})
        names = {c.generated_by_rule for c in generate([endpoint]).cases}
        assert {"case_missing_required_query", "case_wrong_query_type"} <= names


class TestQueryViolation:
    """qagent.modules.generator.values.query_violation"""

    def test_parsed_types_have_a_textual_violation(self):
        assert values.query_violation({"type": "integer"}) == "not-an-integer"
        assert values.query_violation({"type": "number"}) == "not-a-number"
        assert values.query_violation({"type": "boolean"}) == "not-a-boolean"
        assert values.query_violation({"type": ["integer", "null"]}) == "not-an-integer"

    def test_an_enum_is_violated_by_a_value_outside_it(self):
        assert values.query_violation({"type": "string", "enum": ["a", "b"]}) == "qagent-not-in-enum"

    def test_free_text_has_no_wrong_type(self):
        assert values.query_violation({"type": "string"}) is None
        assert values.query_violation({}) is None
        assert values.query_violation(None) is None
