"""Generation and discovery tests."""

from __future__ import annotations

from qagent.modules.discovery.openapi import EndpointSpec, parse_openapi, score_risk
from qagent.modules.generator.rules import generate

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
