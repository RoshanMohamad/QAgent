"""Agent 3's Jest emitter: the declarative document rendered as runnable Jest.

Same load-bearing property as the pytest emitter - nothing from a spec reaches
the output as executable text - checked the same way: by parsing the output
(``node --check``) rather than by eyeballing it. Plus one property the pytest
emitter doesn't need: the JavaScript port of GraphQL error classification must
agree with ``modules/graphql/errors.py`` on every case, or the emitted suite and
QAgent's own runner would disagree about what a crashed resolver is.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from qagent.modules.emitter.jest_emitter import emit, graphql_helpers_source, render_case
from qagent.modules.generator.rules import GeneratedCase
from qagent.modules.graphql.errors import graphql_error_kind, graphql_field_denied

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _case(
    name: str = "GET /orders returns 200",
    endpoint_key: str = "GET /orders",
    spec: dict | None = None,
) -> GeneratedCase:
    return GeneratedCase(
        name=name,
        kind="api_functional",
        endpoint_key=endpoint_key,
        rationale="baseline",
        spec=spec
        or {
            "request": {"method": "GET", "path": "/orders", "auth": "default"},
            "assertions": [{"type": "status_in", "value": [200]}],
            "expectation": "A valid request succeeds.",
        },
    )


def _node_check(path: Path) -> None:
    """Parsing is the real assertion: output node can't parse is not a suite."""
    result = subprocess.run(  # noqa: S603 - fixed argv
        [NODE, "--check", str(path)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


@needs_node
def test_every_assertion_type_renders_to_valid_javascript(tmp_path: Path) -> None:
    assertions = [
        {"type": "status_in", "value": [200, 201]},
        {"type": "status_not_in", "value": [500]},
        {"type": "body_matches", "value": "ok"},
        {"type": "body_not_matches", "value": r"(Traceback \(most recent call last\))"},
        {"type": "header_present", "value": "Content-Type"},
        {"type": "latency_under_ms", "value": 500},
        {"type": "json_path_exists", "value": "data.id"},
        # twice: each gets its own block, so the consts can't collide
        {"type": "json_path_equals", "path": "data.id", "value": 1},
        {"type": "json_path_equals", "path": "data.name", "value": "x"},
        {"type": "graphql_error_kind_in", "value": ["none", "handled"]},
        {"type": "graphql_field_denied", "value": "patrons"},
    ]
    case = _case(spec={"request": {"method": "GET", "path": "/x"}, "assertions": assertions})

    report = emit([case], base_url="http://app.test", out_dir=tmp_path)

    [emitted] = report.files
    source = (tmp_path / emitted.path).read_text(encoding="utf-8")
    assert "function jsonPath" in source
    assert "function graphqlErrorKind" in source
    _node_check(tmp_path / emitted.path)


def test_helpers_are_only_emitted_when_needed(tmp_path: Path) -> None:
    report = emit([_case()], base_url="http://app.test", out_dir=tmp_path, dry_run=True)
    source = report.files[0].content
    assert "function jsonPath" not in source
    assert "graphqlErrorKind" not in source


def test_an_unknown_assertion_fails_loudly_rather_than_passing() -> None:
    case = _case(spec={"request": {"method": "GET", "path": "/x"}, "assertions": [{"type": "?"}]})
    assert "throw new Error(" in render_case(case)


@needs_node
def test_hostile_values_stay_string_literals(tmp_path: Path) -> None:
    hostile = '"); require("child_process").execSync("touch pwned"); ("'
    case = _case(
        name=f"GET {hostile} ",
        spec={
            "request": {
                "method": "POST",
                "path": f"/x{hostile}",
                "json": {"note": f"*/ {hostile} //"},
                "headers": {"x-evil": hostile},
            },
            "assertions": [{"type": "body_matches", "value": hostile}],
            "expectation": f"*/ {hostile}\nprocess.exit(1)",
        },
    )

    report = emit([case], base_url=f"http://app.test{hostile}", out_dir=tmp_path)
    path = tmp_path / report.files[0].path
    source = path.read_text(encoding="utf-8")

    _node_check(path)
    # every occurrence is escaped inside a JSON string, never bare code
    assert 'require("child_process")' not in source
    assert " " not in source
    assert "\nprocess.exit(1)" not in source


def test_files_are_written_one_per_module(tmp_path: Path) -> None:
    cases = [
        _case(endpoint_key="GET /orders"),
        _case(name="GET /orders/{id}", endpoint_key="GET /orders/{id}"),
        _case(name="POST /users", endpoint_key="POST /users"),
    ]

    report = emit(cases, base_url="http://app.test", out_dir=tmp_path)

    assert sorted(f.path for f in report.files) == ["orders.test.js", "users.test.js"]
    assert report.case_count == 3
    assert (tmp_path / "orders.test.js").exists()


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    report = emit([_case()], base_url="http://app.test", out_dir=tmp_path, dry_run=True)
    assert report.case_count == 1
    assert list(tmp_path.iterdir()) == []


def test_unauthenticated_case_does_not_send_credentials() -> None:
    case = _case(spec={"request": {"method": "GET", "path": "/me", "auth": "none"}, "assertions": []})
    lines = [line.strip() for line in render_case(case).splitlines()]
    # the last argument to send() is `authed`
    assert lines[lines.index(");") - 1] == "false,"
    assert "expect(response.status).toBeLessThan(500);" in lines


# ------------------------------------------------------- graphql parity

_GRAPHQL_BODIES: list[object] = [
    None,
    "not json",
    {},
    {"data": {"books": []}},
    {"errors": []},
    {"errors": {}},
    {"errors": "boom"},
    {"errors": [{"message": "Book not found", "extensions": {"code": "NOT_FOUND"}}]},
    {"errors": [{"message": "boom", "extensions": {"code": "INTERNAL_SERVER_ERROR"}}]},
    {"errors": [{"message": "x", "extensions": {"exception": {"stacktrace": ["a"]}}}]},
    {"errors": [{"message": "x", "extensions": {"exception": []}}]},
    {"errors": [{"message": "invalid literal for int() with base 10: 'abc'"}]},
    {"errors": [{"message": "Cannot read properties of undefined (reading 'id')"}]},
    {"errors": [{"message": "Something odd happened"}]},
    {"errors": [{"message": "id is required"}]},
    {"errors": [{"message": 0, "type": "NOT_FOUND"}]},
    {"errors": [{"message": "", "extensions": {"code": ""}, "type": "FORBIDDEN"}]},
    {"errors": ["a string, not an object"]},
    {"errors": [{"message": "id is required"}, {"message": "Something odd"}]},
]

_FIELD_CASES: list[tuple[object, object, str]] = [
    (200, {"data": {"patrons": [{"id": 1}]}}, "patrons"),
    (200, {"data": {"patrons": None}}, "patrons"),
    (200, {"data": {}}, "patrons"),
    (200, {"data": None, "errors": [{"message": "no"}]}, "patrons"),
    (401, {"data": {"patrons": [1]}}, "patrons"),
    (403, None, "patrons"),
    (200, "text", "patrons"),
    (200, {"data": {"patrons": 0}}, "patrons"),
    (200, {"data": {"patrons": []}}, "patrons"),
]


@needs_node
def test_javascript_port_agrees_with_python_on_every_case(tmp_path: Path) -> None:
    script = tmp_path / "parity.js"
    script.write_text(
        graphql_helpers_source()
        + "\nconst input = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n"
        + "process.stdout.write(JSON.stringify({\n"
        + "  kinds: input.bodies.map(graphqlErrorKind),\n"
        + "  denied: input.fields.map(([s, b, f]) => graphqlFieldDenied(s, b, f)),\n"
        + "}));\n",
        encoding="utf-8",
    )
    payload = json.dumps({"bodies": _GRAPHQL_BODIES, "fields": _FIELD_CASES})

    result = subprocess.run(  # noqa: S603 - fixed argv
        [NODE, str(script)], input=payload, capture_output=True, text=True, check=True
    )
    js = json.loads(result.stdout)

    assert js["kinds"] == [graphql_error_kind(body) for body in _GRAPHQL_BODIES]
    assert js["denied"] == [graphql_field_denied(*case) for case in _FIELD_CASES]


# ------------------------------------------------------- framework choice


def test_auto_picks_jest_only_for_a_jest_project(tmp_path: Path) -> None:
    from qagent.cli import _choose_framework

    jest_repo = tmp_path / "node-app"
    jest_repo.mkdir()
    (jest_repo / "package.json").write_text(
        json.dumps({"devDependencies": {"jest": "^30.0.0"}}), encoding="utf-8"
    )
    py_repo = tmp_path / "py-app"
    py_repo.mkdir()
    (py_repo / "requirements.txt").write_text("fastapi\n", encoding="utf-8")

    assert _choose_framework("auto", jest_repo) == "jest"
    assert _choose_framework("auto", py_repo) == "pytest"
    assert _choose_framework("auto", None) == "pytest"
    assert _choose_framework("JEST", None) == "jest"
