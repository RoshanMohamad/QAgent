"""GraphQL support (modules/graphql/, generator/graphql_rules.py) - no network,
no graphql-core: introspection results are built by hand, the same shape any
server returns for the standard query."""

from __future__ import annotations

import httpx
import pytest
import respx

from qagent.modules.discovery.openapi import EndpointSpec
from qagent.modules.generator.graphql_rules import (
    ABSENT_ID,
    MALFORMED_ID,
    graphql_absent_id,
    graphql_explicit_null,
    graphql_happy_path,
    graphql_malformed_id,
)
from qagent.modules.generator.rules import applicable_rules, generate
from qagent.modules.graphql.errors import graphql_error_kind
from qagent.modules.graphql.introspection import (
    fetch_graphql,
    parse_introspection,
    type_literal,
)
from qagent.modules.runner.assertions import evaluate
from qagent.modules.triage.classifier import FailureClass, classify, extract_signals


def _named(kind: str, name: str) -> dict:
    return {"kind": kind, "name": name, "ofType": None}


def _non_null(inner: dict) -> dict:
    return {"kind": "NON_NULL", "name": None, "ofType": inner}


def _list(inner: dict) -> dict:
    return {"kind": "LIST", "name": None, "ofType": inner}


def _arg(name: str, type_ref: dict, default=None) -> dict:
    return {"name": name, "type": type_ref, "defaultValue": default}


def _field(name: str, type_ref: dict, args: list[dict] | None = None) -> dict:
    return {"name": name, "description": None, "args": args or [], "type": type_ref}


BOOK = {
    "kind": "OBJECT",
    "name": "Book",
    "fields": [
        _field("id", _non_null(_named("SCALAR", "ID"))),
        _field("title", _non_null(_named("SCALAR", "String"))),
        _field("author", _named("OBJECT", "Author")),
        _field("excerpt", _named("SCALAR", "String"), [_arg("length", _non_null(_named("SCALAR", "Int")))]),
    ],
    "inputFields": None,
    "enumValues": None,
}

INTROSPECTION = {
    "data": {
        "__schema": {
            "queryType": {"name": "Query"},
            "mutationType": {"name": "Mutation"},
            "types": [
                {
                    "kind": "OBJECT",
                    "name": "Query",
                    "fields": [
                        _field("health", _non_null(_named("SCALAR", "String"))),
                        _field(
                            "book",
                            _named("OBJECT", "Book"),
                            [_arg("id", _non_null(_named("SCALAR", "ID")))],
                        ),
                        _field("__internal", _named("SCALAR", "String")),
                    ],
                    "inputFields": None,
                    "enumValues": None,
                },
                {
                    "kind": "OBJECT",
                    "name": "Mutation",
                    "fields": [
                        _field(
                            "addBook",
                            _non_null(_named("OBJECT", "Book")),
                            [
                                _arg("title", _non_null(_named("SCALAR", "String"))),
                                _arg("tags", _list(_non_null(_named("SCALAR", "String")))),
                                _arg("genre", _named("ENUM", "Genre")),
                            ],
                        ),
                    ],
                    "inputFields": None,
                    "enumValues": None,
                },
                BOOK,
                {"kind": "ENUM", "name": "Genre", "fields": None, "inputFields": None,
                 "enumValues": [{"name": "SF"}, {"name": "FANTASY"}]},
            ],
        }
    }
}


def _endpoints() -> dict[str, EndpointSpec]:
    return {e.key(): e for e in parse_introspection(INTROSPECTION)}


# ----------------------------------------------------------------- introspection


class TestIntrospection:
    def test_every_root_field_becomes_an_endpoint_keyed_by_operation(self):
        assert set(_endpoints()) == {
            "QUERY /graphql/health",
            "QUERY /graphql/book",
            "MUTATION /graphql/addBook",
        }

    def test_introspection_fields_are_skipped(self):
        assert not any("__" in key for key in _endpoints())

    def test_arguments_record_type_nullability_and_list(self):
        args = {a["name"]: a for a in _endpoints()["MUTATION /graphql/addBook"].graphql["args"]}
        assert args["title"] == {
            "name": "title", "type": "String!", "base": "String",
            "kind": "SCALAR", "required": True, "list": False,
        }
        assert args["tags"]["type"] == "[String!]"
        assert args["tags"]["required"] is False and args["tags"]["list"] is True

    def test_selection_takes_scalar_fields_only_and_skips_fields_needing_args(self):
        # author is an object, excerpt needs a required argument - both excluded.
        assert _endpoints()["QUERY /graphql/book"].graphql["selection"] == "{ id title }"

    def test_a_scalar_returning_field_has_no_selection(self):
        assert _endpoints()["QUERY /graphql/health"].graphql["selection"] == ""

    def test_a_mutation_with_an_id_outranks_a_plain_query(self):
        eps = _endpoints()
        assert eps["QUERY /graphql/book"].risk_score > eps["QUERY /graphql/health"].risk_score

    def test_source_is_graphql(self):
        assert all(e.source == "graphql" for e in _endpoints().values())

    @pytest.mark.parametrize("broken", [None, {}, {"data": None}, {"data": {"__schema": {}}}])
    def test_malformed_results_yield_nothing_rather_than_raising(self, broken):
        assert parse_introspection(broken) == []

    def test_type_literal_round_trips_sdl(self):
        assert type_literal(_non_null(_list(_non_null(_named("SCALAR", "String"))))) == "[String!]!"

    @respx.mock
    def test_fetch_probes_until_a_schema_answers(self):
        respx.post("http://app.test/graphql").mock(return_value=httpx.Response(404))
        respx.post("http://app.test/api/graphql").mock(
            return_value=httpx.Response(200, json=INTROSPECTION)
        )
        endpoints, url = fetch_graphql("http://app.test")
        assert url == "http://app.test/api/graphql"
        assert {e.path for e in endpoints} >= {"/api/graphql/book"}

    @respx.mock
    def test_fetch_returns_nothing_when_introspection_is_disabled(self):
        respx.post(url__startswith="http://app.test/").mock(
            return_value=httpx.Response(200, json={"errors": [{"message": "introspection disabled"}]})
        )
        assert fetch_graphql("http://app.test") == ([], None)


# ------------------------------------------------------------------------ errors


class TestErrorKind:
    def test_no_errors(self):
        assert graphql_error_kind({"data": {"book": None}}) == "none"

    @pytest.mark.parametrize(
        "error",
        [
            {"message": "boom", "extensions": {"code": "INTERNAL_SERVER_ERROR"}},
            {"message": "boom", "extensions": {"exception": {"stacktrace": ["..."]}}},
            {"message": "invalid literal for int() with base 10: 'x'"},
            {"message": "'NoneType' object has no attribute 'strip'"},
            {"message": "can only join an iterable"},
            {"message": "Cannot read properties of undefined (reading 'title')"},
        ],
    )
    def test_internal(self, error):
        assert graphql_error_kind({"errors": [error]}) == "internal"

    @pytest.mark.parametrize(
        "error",
        [
            {"message": "id must be numeric", "extensions": {"code": "BAD_USER_INPUT"}},
            {"message": "Could not resolve", "type": "NOT_FOUND"},  # GitHub's shape
            {"message": "Book not found"},  # codeless, but reads as a client error
        ],
    )
    def test_handled(self, error):
        assert graphql_error_kind({"errors": [error]}) == "handled"

    def test_codeless_and_unreadable_is_unclear_not_internal(self):
        # The conservative answer: not evidence of a crash (ADR-0003).
        assert graphql_error_kind({"errors": [{"message": "Something happened"}]}) == "unclear"

    def test_one_internal_error_among_handled_ones_is_internal(self):
        body = {"errors": [
            {"message": "x", "extensions": {"code": "NOT_FOUND"}},
            {"message": "boom", "extensions": {"code": "INTERNAL_SERVER_ERROR"}},
        ]}
        assert graphql_error_kind(body) == "internal"

    @pytest.mark.parametrize("body", [None, "text", {"errors": "not-a-list"}])
    def test_unparseable_bodies_are_unclear(self, body):
        assert graphql_error_kind(body) == "unclear"

    def test_assertion_reports_the_kind_and_the_first_message(self):
        outcome = evaluate(
            {"type": "graphql_error_kind_in", "value": ["none", "handled"]},
            {"status": 200, "body_json": {"errors": [{"message": "can only join an iterable"}]}},
        )
        assert outcome.passed is False
        assert outcome.actual == "internal"
        assert "can only join an iterable" in outcome.message


# ------------------------------------------------------------------------- rules


class TestGraphqlRules:
    def test_happy_path_sends_required_arguments_and_omits_optional_ones(self):
        case = graphql_happy_path(_endpoints()["MUTATION /graphql/addBook"])
        body = case.spec["request"]["json"]
        assert body["variables"] == {"title": "qagent-title"}
        assert body["query"] == (
            "mutation QAgent($title: String!) { addBook(title: $title) { id title } }"
        )
        assert case.spec["request"]["method"] == "POST"
        assert case.spec["request"]["path"] == "/graphql"

    def test_happy_path_expects_no_errors_at_all(self):
        case = graphql_happy_path(_endpoints()["QUERY /graphql/health"])
        kinds = [a for a in case.spec["assertions"] if a["type"] == "graphql_error_kind_in"]
        assert kinds == [{"type": "graphql_error_kind_in", "value": ["none"]}]
        assert case.spec["negative"] is False

    def test_malformed_and_absent_ids_accept_null_or_a_handled_error(self):
        book = _endpoints()["QUERY /graphql/book"]
        malformed = graphql_malformed_id(book)
        absent = graphql_absent_id(book)
        assert malformed.spec["request"]["json"]["variables"] == {"id": MALFORMED_ID}
        assert absent.spec["request"]["json"]["variables"] == {"id": ABSENT_ID}
        for case in (malformed, absent):
            assert case.spec["negative"] is True
            assert {"type": "graphql_error_kind_in", "value": ["none", "handled"]} in case.spec[
                "assertions"
            ]

    def test_id_rules_skip_fields_without_an_id_argument(self):
        health = _endpoints()["QUERY /graphql/health"]
        assert graphql_malformed_id(health) is None
        assert graphql_absent_id(health) is None

    def test_explicit_null_targets_the_first_optional_argument(self):
        case = graphql_explicit_null(_endpoints()["MUTATION /graphql/addBook"])
        assert case.spec["request"]["json"]["variables"] == {"title": "qagent-title", "tags": None}
        assert "$tags: [String!]" in case.spec["request"]["json"]["query"]

    def test_explicit_null_skips_fields_with_only_required_arguments(self):
        assert graphql_explicit_null(_endpoints()["QUERY /graphql/book"]) is None

    def test_rest_rules_never_run_on_graphql_fields_and_vice_versa(self):
        rest = EndpointSpec(method="GET", path="/health", responses={"200": {}})
        graphql = _endpoints()["QUERY /graphql/book"]
        assert all(name.startswith("graphql_") for name in applicable_rules(graphql))
        assert not any(name.startswith("graphql_") for name in applicable_rules(rest))

    def test_generation_is_deterministic(self):
        endpoints = list(_endpoints().values())
        first = [c.to_dict() for c in generate(endpoints).cases]
        second = [c.to_dict() for c in generate(endpoints).cases]
        assert first == second


# ------------------------------------------------------------------------ triage


def _signals(case, body_json, history=None):
    return extract_signals(
        spec=case.spec,
        request=case.spec["request"],
        response={"status": 200, "body_json": body_json, "body_text": str(body_json)},
        failure_message=None,
        recent_history=history,
    )


class TestGraphqlTriage:
    def test_an_internal_error_on_edge_input_is_a_real_bug(self):
        case = graphql_malformed_id(_endpoints()["QUERY /graphql/book"])
        verdict = classify(_signals(case, {"errors": [{"message": "invalid literal for int()"}]}))
        assert verdict.failure_class is FailureClass.REAL_BUG
        assert verdict.confidence == 0.90

    def test_an_internal_error_on_a_valid_request_is_a_real_bug(self):
        case = graphql_happy_path(_endpoints()["MUTATION /graphql/addBook"])
        verdict = classify(_signals(case, {"errors": [{"message": "can only join an iterable"}]}))
        assert verdict.failure_class is FailureClass.REAL_BUG

    def test_an_unclear_error_is_never_reported(self):
        case = graphql_malformed_id(_endpoints()["QUERY /graphql/book"])
        verdict = classify(_signals(case, {"errors": [{"message": "Something happened"}]}))
        assert verdict.failure_class is FailureClass.UNKNOWN

    def test_flaky_history_does_not_excuse_an_unhandled_exception(self):
        case = graphql_malformed_id(_endpoints()["QUERY /graphql/book"])
        signals = _signals(
            case,
            {"errors": [{"message": "x", "extensions": {"code": "INTERNAL_SERVER_ERROR"}}]},
            history=["passed", "failed", "passed"],
        )
        assert classify(signals).failure_class is FailureClass.REAL_BUG


# ----------------------------------------------------------------- bug report + emit


def test_offline_bug_report_quotes_the_operation_and_the_graphql_error():
    from qagent.config import get_settings
    from qagent.modules.llm.budget import Budget
    from qagent.modules.llm.client import LlmClient
    from qagent.modules.llm.providers import NullProvider
    from qagent.modules.triage.agent import build_bug_report

    offline = LlmClient(settings=get_settings(), provider=NullProvider(), budget=Budget(10, 1000, 1.0))
    case = graphql_happy_path(_endpoints()["MUTATION /graphql/addBook"])
    response = {"status": 200, "body_json": {"errors": [{"message": "can only join an iterable"}]}}
    verdict = classify(_signals(case, response["body_json"]))
    report = build_bug_report(
        case_name=case.name,
        verdict=verdict,
        spec=case.spec,
        request=case.spec["request"],
        response=response,
        failure_message=None,
        llm=offline,
    )
    assert "can only join an iterable" in report["actual"]
    assert any("addBook" in step for step in report["steps"])
    assert "resolver" in report["suggested_fix"]


def test_emitted_graphql_suite_is_standalone_python():
    from qagent.modules.emitter.pytest_emitter import emit

    cases = generate(list(_endpoints().values())).cases
    report = emit(cases, base_url="http://app.test", out_dir=None, dry_run=True)
    (graphql_file,) = [f for f in report.files if f.path == "test_graphql.py"]
    compile(graphql_file.content, graphql_file.path, "exec")  # must not raise
    assert "def graphql_error_kind(" in graphql_file.content
    assert "from qagent" not in graphql_file.content
